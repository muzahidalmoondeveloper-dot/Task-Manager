"""Regression tests for the Microsoft/Teams automation pipeline
(Automation Pipeline Audit follow-up) — exercises the real
`sync_microsoft_data_for_user` -> `analyze_pending_sources_for_user`
pipeline end to end for a Teams meeting transcript, with the Graph HTTP
calls mocked (no live Microsoft/Graph credentials are available in this
environment — see the final report) and the LLM provider mocked via the
same `_ScriptedProvider` pattern used elsewhere in this suite.

Covers (spec's "MICROSOFT / TEAMS TEST" + "TRANSCRIPT DELAY TEST"):
  1. A meeting is discovered with an onlineMeeting id but Graph returns
     NO transcript yet -> the CalendarEvent is marked
     transcript_status="waiting" with a scheduled retry
     (transcript_next_check_at), NOT a permanent failure.
  2. Once the retry is due and Graph now returns the transcript
     ("Suzon will prepare the Q3 report by Friday."), a second sync
     fetches and stores it, analysis runs, and a Task is created with
     Task provenance (TaskSource: provider=microsoft,
     source_type=microsoft_teams_transcript).
  3. Re-running sync + analysis afterward creates NO second Task for the
     same transcript (idempotency).

Runs against the real database connection the app uses. Every row this
test creates is deleted before it returns.
"""

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete, select, update as sql_update
from sqlalchemy.orm import selectinload

from app.core.org_roles import OWNER, TEAM_MEMBER
from app.core.database import AsyncSessionLocal, engine
from app.models.integration import CalendarEvent, IntegrationAccount, MeetingTranscript, TaskSource
from app.models.organization import Organization, OrganizationMembership
from app.models.task import Task
from app.models.user import User
from app.services.automation_tasks import analyze_pending_sources_for_user, sync_microsoft_data_for_user
from app.services.integrations.microsoft_graph import MicrosoftGraphService
from app.services.llm.base import LLMProvider, LLMResponse


class _ScriptedProvider(LLMProvider):
    def __init__(self, replies: list[str]):
        self._replies = list(replies)
        self.call_count = 0

    async def generate_text(self, **kwargs):
        reply = self._replies[min(self.call_count, len(self._replies) - 1)]
        self.call_count += 1
        return LLMResponse(text=reply)


def _payload(tasks: list[dict]) -> str:
    return json.dumps({
        "should_create_tasks": bool(tasks),
        "reason": "clear action item" if tasks else "no action items",
        "source_category": "meeting_followup" if tasks else "internal_update",
        "tasks": tasks,
    })


async def _scenario(monkeypatch):
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]

        owner = User(full_name="TeamsPipe Owner", email=f"teamspipe.owner.{suffix}@example-corp.com", hashed_password="x", role=OWNER)
        suzon = User(full_name="Suzon", email=f"suzon.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)
        db.add_all([owner, suzon])
        await db.commit()
        for u in (owner, suzon):
            await db.refresh(u)

        org = Organization(name=f"TeamsPipe Org {suffix}", slug=f"teamspipe-org-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.commit()
        org = (await db.execute(select(Organization).options(selectinload(Organization.subscription)).where(Organization.id == org.id))).scalar_one()

        db.add_all([
            OrganizationMembership(organization_id=org.id, user_id=owner.id, role=OWNER),
            OrganizationMembership(organization_id=org.id, user_id=suzon.id, role=TEAM_MEMBER),
        ])
        await db.commit()
        owner.last_active_organization_id = org.id
        await db.commit()

        account = IntegrationAccount(
            user_id=owner.id, organization_id=org.id, provider="microsoft",
            account_email=f"teamspipe.{suffix}@outlook.com", access_token="fake-access-token", refresh_token=None,
        )
        db.add(account)
        await db.commit()
        await db.refresh(account)

        now = datetime.now(timezone.utc)
        online_meeting_id = f"om-{suffix}"
        event_id = f"evt-{suffix}"
        event_dict = {
            "id": event_id,
            "subject": "Weekly Finance Review",
            "organizer": {"emailAddress": {"address": owner.email}},
            "attendees": [],
            "start": {"dateTime": now.isoformat()},
            "end": {"dateTime": (now + timedelta(hours=1)).isoformat()},
            "onlineMeeting": {"id": online_meeting_id},
            "onlineMeetingUrl": f"https://teams.microsoft.com/l/meetup-join/{suffix}",
            "webLink": None,
        }

        transcript_state = {"available": False}

        async def fake_list_messages(self, start_at, end_at):
            return []

        async def fake_list_calendar_events(self, start_at, end_at):
            return [event_dict]

        async def fake_list_transcripts_for_online_meeting(self, meeting_id):
            assert meeting_id == online_meeting_id
            if transcript_state["available"]:
                return [{"id": f"transcript-{suffix}", "createdDateTime": now.isoformat()}]
            return []

        async def fake_get_transcript_content(self, meeting_id, transcript_id):
            return "Suzon will prepare the Q3 report by Friday."

        monkeypatch.setattr(MicrosoftGraphService, "ensure_fresh_token", lambda self, db: _noop())
        monkeypatch.setattr(MicrosoftGraphService, "list_messages", fake_list_messages)
        monkeypatch.setattr(MicrosoftGraphService, "list_calendar_events", fake_list_calendar_events)
        monkeypatch.setattr(MicrosoftGraphService, "list_transcripts_for_online_meeting", fake_list_transcripts_for_online_meeting)
        monkeypatch.setattr(MicrosoftGraphService, "get_transcript_content", fake_get_transcript_content)

        task_item = {
            "title": "Prepare the Q3 report",
            "description": None,
            "suggested_start_date": None,
            "suggested_due_date": None,
            "suggested_assignee_name": "Suzon",
            "suggested_assignee_email": None,
            "suggested_project_name": None,
            "suggested_team_name": None,
            "confidence": "high",
        }
        monkeypatch.setattr(
            "app.services.ai_task_extractor.get_llm_provider",
            lambda: _ScriptedProvider([_payload([task_item])]),
        )

        created_task_ids: list[int] = []

        try:
            # ── 1. Meeting discovered, transcript not ready yet ->
            # waiting_for_transcript with a scheduled retry, not a
            # permanent failure. ─────────────────────────────────────────
            sync_result = await sync_microsoft_data_for_user(db=db, user=owner)
            assert sync_result["calendar_events_imported"] == 1
            assert sync_result["transcripts_imported"] == 0

            saved_event = (await db.execute(select(CalendarEvent).where(CalendarEvent.provider_event_id == event_id))).scalar_one()
            assert saved_event.transcript_status == "waiting"
            assert saved_event.transcript_attempts == 1
            assert saved_event.transcript_next_check_at is not None, "a bounded retry must be scheduled, not an immediate permanent failure"

            no_transcripts_yet = (await db.execute(select(MeetingTranscript).where(MeetingTranscript.calendar_event_id == saved_event.id))).scalars().all()
            assert len(no_transcripts_yet) == 0

            # Re-syncing again immediately (backoff not due yet) must not
            # re-check the transcript before its scheduled time.
            await sync_microsoft_data_for_user(db=db, user=owner)
            still_waiting = (await db.execute(select(CalendarEvent).where(CalendarEvent.provider_event_id == event_id))).scalar_one()
            assert still_waiting.transcript_attempts == 1, "must not re-check before transcript_next_check_at"

            # ── 2. Backdate the retry time (simulating time passing) and
            # make the transcript available -> a resync fetches it,
            # analysis runs, a Task is created with provenance. ─────────────
            await db.execute(
                sql_update(CalendarEvent)
                .where(CalendarEvent.id == saved_event.id)
                .values(transcript_next_check_at=now - timedelta(minutes=1))
            )
            await db.commit()
            transcript_state["available"] = True

            resync_result = await sync_microsoft_data_for_user(db=db, user=owner)
            assert resync_result["transcripts_imported"] == 1

            transcript_row = (await db.execute(select(MeetingTranscript).where(MeetingTranscript.calendar_event_id == saved_event.id))).scalar_one()
            assert transcript_row.transcript_text == "Suzon will prepare the Q3 report by Friday."

            refreshed_event = (await db.execute(select(CalendarEvent).where(CalendarEvent.id == saved_event.id))).scalar_one()
            assert refreshed_event.transcript_status == "available"

            ai_result = await analyze_pending_sources_for_user(db=db, user=owner, org_id=org.id)
            assert ai_result["tasks_created"] == 1

            task = (await db.execute(select(Task).where(Task.name == "Prepare the Q3 report"))).scalar_one()
            created_task_ids.append(task.id)
            assert task.assignee_id == suzon.id, "the transcript's named assignee must resolve to this org's own Suzon"
            assert task.organization_id == org.id

            provenance = (await db.execute(select(TaskSource).where(TaskSource.task_id == task.id))).scalar_one()
            assert provenance.provider == "microsoft"
            assert provenance.source_type == "microsoft_teams_transcript"
            assert provenance.source_external_id == f"transcript-{suffix}"

            # ── 3. Re-sync + re-analyze must never create a second Task
            # for the same transcript. ───────────────────────────────────
            await sync_microsoft_data_for_user(db=db, user=owner)
            second_ai_result = await analyze_pending_sources_for_user(db=db, user=owner, org_id=org.id)
            assert second_ai_result["tasks_created"] == 0
            all_tasks_named = (await db.execute(select(Task).where(Task.name == "Prepare the Q3 report"))).scalars().all()
            assert len(all_tasks_named) == 1

        finally:
            if created_task_ids:
                await db.execute(delete(TaskSource).where(TaskSource.task_id.in_(created_task_ids)))
                await db.execute(delete(Task).where(Task.id.in_(created_task_ids)))
            event_ids_result = await db.execute(select(CalendarEvent.id).where(CalendarEvent.integration_account_id == account.id))
            event_ids = [row[0] for row in event_ids_result.all()]
            if event_ids:
                await db.execute(delete(MeetingTranscript).where(MeetingTranscript.calendar_event_id.in_(event_ids)))
            await db.execute(delete(CalendarEvent).where(CalendarEvent.integration_account_id == account.id))
            await db.execute(delete(IntegrationAccount).where(IntegrationAccount.id == account.id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id == org.id))
            await db.execute(delete(User).where(User.id.in_([owner.id, suzon.id])))
            await db.commit()

    await engine.dispose()


async def _noop():
    return None


def test_microsoft_teams_automation_pipeline(monkeypatch):
    asyncio.run(_scenario(monkeypatch))
