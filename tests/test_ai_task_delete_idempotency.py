"""Regression test: deleting an AI-generated Task must never cause the
same source (Gmail message / Teams transcript) to recreate it on the
next sync (Automation Pipeline + Task ownership follow-up, item 12 of
the spec's regression matrix).

ROOT CAUSE CHECKED (not found to be broken): `TaskSource.task_id` is
`ondelete="CASCADE"` — deleting the Task also deletes its `TaskSource`
provenance row. This looked like a plausible re-arm risk, but the
ACTUAL idempotency guard `_claim_source_rows`/`analyze_pending_sources_
for_user` reads and writes is `ImportedEmail.processing_status` (and
`MeetingTranscript.processing_status`), never `TaskSource` — a row is
only ever re-claimed for analysis while `processing_status ==
"discovered"`. Once analysis runs and creates a Task, `processing_status`
is set to a terminal value (`task_created`/`partially_created`) and stays
there permanently — deleting the resulting Task afterward does not touch
`ImportedEmail`/`MeetingTranscript` at all, so the terminal
`processing_status` (the actual re-claim guard) survives the delete
completely unchanged. This test proves that end to end rather than by
inspection alone.

Also verifies the Personal-Task-owner delete fix (this same follow-up)
is what actually lets the TM delete their own AI-generated Task in the
first place — before that fix, `delete_task` would have 403'd for a
team-less, TM-owned AI Task.

Runs against the real database connection the app uses (Gmail HTTP calls
mocked — no live Gmail credentials are available in this environment).
Every row this test creates is deleted before it returns.
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from app.api.routes.tasks import delete_task
from app.core.org_roles import OWNER, TEAM_MANAGER
from app.core.database import AsyncSessionLocal, engine
from app.core.tenant import TenantContext
from app.models.integration import ImportedEmail, IntegrationAccount, TaskSource
from app.models.organization import Organization, OrganizationMembership
from app.models.task import Task
from app.models.user import User
from app.services.automation_tasks import analyze_pending_sources_for_user, sync_gmail_data_for_user
from app.services.integrations.gmail import GmailService
from app.services.llm.base import LLMProvider, LLMResponse


class _ScriptedProvider(LLMProvider):
    def __init__(self, reply: str):
        self._reply = reply

    async def generate_text(self, **kwargs):
        return LLMResponse(text=self._reply)


def _payload() -> str:
    return json.dumps({
        "should_create_tasks": True,
        "reason": "clear action item",
        "source_category": "work_action",
        "tasks": [{
            "title": "Prepare the board deck",
            "description": None,
            "suggested_start_date": None,
            "suggested_due_date": None,
            "suggested_assignee_name": None,
            "suggested_assignee_email": None,
            "suggested_project_name": None,
            "suggested_team_name": None,
            "confidence": "high",
        }],
    })


async def _scenario(monkeypatch):
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]

        owner = User(full_name="AIDel Owner", email=f"aidel.owner.{suffix}@example-corp.com", hashed_password="x", role=OWNER)
        tm = User(full_name="AIDel TM", email=f"aidel.tm.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MANAGER)
        db.add_all([owner, tm])
        await db.commit()
        for u in (owner, tm):
            await db.refresh(u)

        org = Organization(name=f"AIDel Org {suffix}", slug=f"aidel-org-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.commit()
        org = (await db.execute(select(Organization).options(selectinload(Organization.subscription)).where(Organization.id == org.id))).scalar_one()

        db.add_all([
            OrganizationMembership(organization_id=org.id, user_id=owner.id, role=OWNER),
            OrganizationMembership(organization_id=org.id, user_id=tm.id, role=TEAM_MANAGER),
        ])
        await db.commit()
        membership_tm = (await db.execute(select(OrganizationMembership).where(OrganizationMembership.organization_id == org.id, OrganizationMembership.user_id == tm.id))).scalar_one()
        tm.last_active_organization_id = org.id
        await db.commit()

        account = IntegrationAccount(
            user_id=tm.id, organization_id=org.id, provider="google",
            account_email=f"aidel.{suffix}@gmail.com", access_token="fake", refresh_token=None,
        )
        db.add(account)
        await db.commit()
        await db.refresh(account)

        message = {
            "id": f"msg-{suffix}",
            "subject": "Board deck",
            "from": {"emailAddress": {"address": f"ceo.{suffix}@example.com"}},
            "toRecipients": [],
            "receivedDateTime": datetime.now(timezone.utc).isoformat(),
            "bodyPreview": "Please prepare the board deck for Monday.",
            "body": {"content": "Please prepare the board deck for Monday."},
        }

        async def fake_list_messages(self, start_at, end_at):
            return [message]

        monkeypatch.setattr(GmailService, "list_messages", fake_list_messages)
        monkeypatch.setattr(GmailService, "ensure_fresh_token", lambda self, db: _noop())
        monkeypatch.setattr("app.services.ai_task_extractor.get_llm_provider", lambda: _ScriptedProvider(_payload()))

        tm_tenant = TenantContext(organization_id=org.id, organization=org, membership=membership_tm, user=tm, db=db)

        created_task_ids: list[int] = []

        try:
            await sync_gmail_data_for_user(db=db, user=tm)
            ai_result = await analyze_pending_sources_for_user(db=db, user=tm, org_id=org.id)
            assert ai_result["tasks_created"] == 1

            task = (await db.execute(select(Task).where(Task.name == "Prepare the board deck"))).scalar_one()
            created_task_ids.append(task.id)
            assert task.assignee_id == tm.id
            assert task.team_id is None, "an AI Task with no resolvable Team must be a Personal Task"

            provenance_before = (await db.execute(select(TaskSource).where(TaskSource.task_id == task.id))).scalar_one()
            assert provenance_before.ai_generated is True

            email_status_before = (await db.execute(select(ImportedEmail.processing_status).where(ImportedEmail.provider_message_id == message["id"]))).scalar_one()
            assert email_status_before == "task_created"

            # The Personal-Task-owner delete fix is what makes this
            # possible at all — before it, a team-less AI Task's own
            # assignee had no delete authority whatsoever.
            await delete_task(task.id, tenant=tm_tenant)
            deleted_check = (await db.execute(select(Task).where(Task.id == task.id))).scalar_one_or_none()
            assert deleted_check is None
            created_task_ids.remove(task.id)

            # TaskSource cascades away with the Task (expected/harmless —
            # there's no Task left for it to describe provenance for).
            provenance_after = (await db.execute(select(TaskSource).where(TaskSource.task_id == task.id))).scalar_one_or_none()
            assert provenance_after is None

            # The REAL idempotency guard — ImportedEmail.processing_status
            # — is untouched by the Task deletion.
            email_status_after = (await db.execute(select(ImportedEmail.processing_status).where(ImportedEmail.provider_message_id == message["id"]))).scalar_one()
            assert email_status_after == "task_created", "deleting the Task must never re-arm the source's processing_status"

            # Re-sync (the message is still returned by Gmail, as it
            # would be on a real subsequent poll) + re-analyze must NOT
            # recreate the Task.
            await sync_gmail_data_for_user(db=db, user=tm)
            second_ai_result = await analyze_pending_sources_for_user(db=db, user=tm, org_id=org.id)
            assert second_ai_result["tasks_created"] == 0, "a deleted AI Task must never be recreated by a later resync"
            assert second_ai_result["sources_analyzed"] == 0, "an already-terminally-processed email must never be re-claimed for analysis"

            recreated = (await db.execute(select(Task).where(Task.name == "Prepare the board deck"))).scalars().all()
            assert len(recreated) == 0

        finally:
            if created_task_ids:
                await db.execute(delete(TaskSource).where(TaskSource.task_id.in_(created_task_ids)))
                await db.execute(delete(Task).where(Task.id.in_(created_task_ids)))
            await db.execute(delete(ImportedEmail).where(ImportedEmail.integration_account_id == account.id))
            await db.execute(delete(IntegrationAccount).where(IntegrationAccount.id == account.id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id == org.id))
            await db.execute(delete(User).where(User.id.in_([owner.id, tm.id])))
            await db.commit()

    await engine.dispose()


async def _noop():
    return None


def test_ai_task_delete_does_not_regenerate_on_resync(monkeypatch):
    asyncio.run(_scenario(monkeypatch))
