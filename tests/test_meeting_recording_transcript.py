"""Regression test for the live-meeting recording -> transcript -> AI task
extraction flow (POST /meetings/{id}/recording/start and .../stop).

No audio or transcription vendor is involved on the backend at all — the
browser produces the transcript text client-side, and the backend's only
job is: store it, hand it to the existing (already provider-agnostic)
AITaskExtractor, and create real meeting to-dos for whatever clear action
items come back. This test scripts the LLM provider (same pattern as
test_ai_task_extractor.py) so it never depends on a live Ollama/OpenAI/etc
connection, and proves:
  - a clear action item becomes a real Task + MeetingTask, with the
    assignee resolved from a known participant name and the due date
    carried over;
  - a non-actionable transcript creates nothing;
  - re-submitting the same (or an extended) transcript never creates a
    duplicate to-do for a title that already exists on the meeting.

Runs against the real database. Every row this test creates is deleted
before it returns.
"""

import asyncio
import json
import uuid
from datetime import date, datetime, timedelta, timezone

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete

from app.api.routes.meetings import create_meeting, start_recording, stop_recording
from app.core.database import AsyncSessionLocal, engine
from app.core.tenant import TenantContext
from app.models.meeting import Meeting, MeetingTask
from app.models.organization import Organization, OrganizationMembership
from app.models.task import Task
from app.models.user import User
from app.schemas.meeting import MeetingCreate, TranscriptSubmit
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


_TASK_ITEM = {
    "title": "Send the updated proposal to the client",
    "description": None,
    "suggested_start_date": None,
    "suggested_due_date": (date.today() + timedelta(days=3)).isoformat(),
    "suggested_assignee_name": "Recording Member",
    "suggested_assignee_email": None,
    "suggested_project_name": None,
    "suggested_team_name": None,
    "confidence": "high",
}


async def _scenario(monkeypatch):
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="Recording Owner", email=f"recording.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        member = User(full_name="Recording Member", email=f"recording.member.{suffix}@test.invalid", hashed_password="x", role="team_member")
        db.add_all([owner, member])
        await db.flush()

        org = Organization(name=f"Recording Org {suffix}", slug=f"recording-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.flush()

        owner_membership = OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner")
        member_membership = OrganizationMembership(organization_id=org.id, user_id=member.id, role="team_member")
        db.add_all([owner_membership, member_membership])
        await db.commit()

        when = datetime.now(timezone.utc) + timedelta(days=1)
        owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=owner_membership, user=owner, db=db)

        meeting = await create_meeting(
            MeetingCreate(title=f"Recording Test Meeting {suffix}", scheduled_at=when, meeting_type="level_10"),
            db=db, tenant=owner_tenant,
        )
        created_task_ids: list[int] = []

        try:
            started = await start_recording(meeting.id, db=db, tenant=owner_tenant)
            assert started.is_recording is True

            # 1. A transcript with one clear action item mentioning a real
            # participant by name and a relative due date.
            monkeypatch.setattr(
                "app.services.ai_task_extractor.get_llm_provider",
                lambda: _ScriptedProvider([_payload([_TASK_ITEM])]),
            )
            result = await stop_recording(
                meeting.id,
                TranscriptSubmit(text="Owner: I'll get the proposal ready. Member: I'll send the updated proposal to the client by Friday."),
                db=db, tenant=owner_tenant,
            )
            assert result.meeting.is_recording is False
            assert result.meeting.transcript_text.startswith("Owner:")
            assert len(result.tasks_created) == 1
            created = result.tasks_created[0]
            assert created.title == _TASK_ITEM["title"]
            assert created.assignee_id == member.id, "assignee should resolve to the participant named in the transcript"
            assert created.due_date == date.today() + timedelta(days=3)

            assert len(result.meeting.meeting_tasks) == 1
            created_task_ids.append(result.meeting.meeting_tasks[0].task_id)

            # 2. Re-stopping with the same transcript (simulating a second
            # start/stop cycle that re-submits the accumulated text) must
            # not create a second to-do for the same title.
            monkeypatch.setattr(
                "app.services.ai_task_extractor.get_llm_provider",
                lambda: _ScriptedProvider([_payload([_TASK_ITEM])]),
            )
            result2 = await stop_recording(
                meeting.id,
                TranscriptSubmit(text="Owner: I'll get the proposal ready. Member: I'll send the updated proposal to the client by Friday."),
                db=db, tenant=owner_tenant,
            )
            assert result2.tasks_created == [], "an already-created title must never be duplicated"
            assert len(result2.meeting.meeting_tasks) == 1

            # 3. A transcript with no actionable content creates nothing.
            monkeypatch.setattr(
                "app.services.ai_task_extractor.get_llm_provider",
                lambda: _ScriptedProvider([_payload([])]),
            )
            result3 = await stop_recording(
                meeting.id,
                TranscriptSubmit(text="Just a friendly catch-up with no decisions or action items."),
                db=db, tenant=owner_tenant,
            )
            assert result3.tasks_created == []
            assert len(result3.meeting.meeting_tasks) == 1, "still just the one from step 1"

        finally:
            await db.execute(delete(MeetingTask).where(MeetingTask.meeting_id == meeting.id))
            if created_task_ids:
                await db.execute(delete(Task).where(Task.id.in_(created_task_ids)))
            await db.execute(delete(Meeting).where(Meeting.id == meeting.id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id == org.id))
            await db.execute(delete(User).where(User.id.in_([owner.id, member.id])))
            await db.commit()

    await engine.dispose()


def test_recording_stop_extracts_and_dedupes_tasks(monkeypatch):
    asyncio.run(_scenario(monkeypatch))
