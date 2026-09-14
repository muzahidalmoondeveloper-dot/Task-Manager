"""Regression tests for the Gmail automation pipeline (Automation
Pipeline Audit follow-up, Phase 2) — Gmail had NO fetch/analysis path at
all before this: only OAuth connect/callback existed. This exercises the
real `sync_gmail_data_for_user` -> `analyze_pending_sources_for_user`
pipeline end to end, with the Gmail HTTP calls mocked (no live Gmail
credentials are available in this environment — see the final report's
"anything not live-tested" section) and the LLM provider mocked via the
same `_ScriptedProvider` pattern test_automation_assignee_tenant_isolation.py
already established.

Covers:
  1. An actionable email ("Please prepare the monthly sales report by
     September 20.") -> fetched -> normalized -> analyzed -> a valid Task
     created -> provenance (TaskSource) stored -> ImportedEmail marked
     task_created.
  2. A newsletter/FYI email -> no Task (caught by the existing keyword
     pre-filter before AI is even called).
  3. Re-running sync + analysis for the SAME Gmail message (simulating a
     second manual sync click) creates NO second Task — the upsert
     never resets processing_status, and analyze only ever claims rows
     still in "discovered".

Runs against the real database connection the app uses. Every row this
test creates is deleted before it returns.
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from app.core.org_roles import OWNER
from app.core.database import AsyncSessionLocal, engine
from app.models.integration import ImportedEmail, IntegrationAccount, TaskSource
from app.models.organization import Organization, OrganizationMembership
from app.models.task import Task
from app.models.user import User
from app.services.automation_tasks import analyze_pending_sources_for_user, sync_gmail_data_for_user
from app.services.integrations.gmail import GmailService
from app.services.llm.base import LLMProvider, LLMResponse


class _ScriptedProvider(LLMProvider):
    def __init__(self, replies: list[str]):
        self._replies = list(replies)
        self.call_count = 0

    async def generate_text(self, **kwargs):
        reply = self._replies[min(self.call_count, len(self._replies) - 1)]
        self.call_count += 1
        return LLMResponse(text=reply)


def _payload(tasks: list[dict], category: str = "work_action") -> str:
    return json.dumps({
        "should_create_tasks": bool(tasks),
        "reason": "clear action item" if tasks else "no action items",
        "source_category": category,
        "tasks": tasks,
    })


def _make_message(message_id: str, subject: str, body: str, sender: str) -> dict:
    """Already in the normalized (Graph-like) shape GmailService._normalize_message
    would produce — this test mocks GmailService.list_messages at the
    adapter boundary, exactly where a provider adapter should be mocked,
    rather than mocking raw Gmail JSON + base64 decoding internals."""
    return {
        "id": message_id,
        "subject": subject,
        "from": {"emailAddress": {"address": sender}},
        "toRecipients": [],
        "receivedDateTime": datetime.now(timezone.utc).isoformat(),
        "bodyPreview": body[:200],
        "body": {"content": body},
    }


async def _scenario(monkeypatch):
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]

        owner = User(full_name="GmailPipe Owner", email=f"gmailpipe.owner.{suffix}@example-corp.com", hashed_password="x", role=OWNER)
        db.add(owner)
        await db.commit()
        await db.refresh(owner)

        org = Organization(name=f"GmailPipe Org {suffix}", slug=f"gmailpipe-org-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.commit()
        org = (await db.execute(select(Organization).options(selectinload(Organization.subscription)).where(Organization.id == org.id))).scalar_one()

        db.add(OrganizationMembership(organization_id=org.id, user_id=owner.id, role=OWNER))
        await db.commit()
        owner.last_active_organization_id = org.id
        await db.commit()

        account = IntegrationAccount(
            user_id=owner.id, organization_id=org.id, provider="google",
            account_email=f"gmailpipe.{suffix}@gmail.com", access_token="fake-access-token", refresh_token=None,
        )
        db.add(account)
        await db.commit()
        await db.refresh(account)

        actionable_message = _make_message(
            f"msg-actionable-{suffix}", "Monthly sales report",
            "Please prepare the monthly sales report by September 20.",
            f"client.{suffix}@example.com",
        )
        newsletter_message = _make_message(
            f"msg-newsletter-{suffix}", "Weekly Newsletter — unsubscribe anytime",
            "Check out our latest promotion and discount codes! Newsletter content here.",
            f"noreply.{suffix}@marketing.example.com",
        )

        call_state = {"messages": [actionable_message, newsletter_message]}

        async def fake_list_messages(self, start_at, end_at):
            return call_state["messages"]

        monkeypatch.setattr(GmailService, "list_messages", fake_list_messages)
        monkeypatch.setattr(GmailService, "ensure_fresh_token", lambda self, db: _noop())

        task_item = {
            "title": "Prepare monthly sales report",
            "description": "Prepare and send the monthly sales report.",
            "suggested_start_date": None,
            "suggested_due_date": "2026-09-20",
            "suggested_assignee_name": None,
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
            # ── 1. Fetch: both messages land as ImportedEmail rows. ─────────
            sync_result = await sync_gmail_data_for_user(db=db, user=owner)
            assert sync_result["emails_imported"] == 2

            emails = (await db.execute(select(ImportedEmail).where(ImportedEmail.integration_account_id == account.id))).scalars().all()
            assert len(emails) == 2
            assert all(e.processing_status == "discovered" for e in emails)

            # ── Analyze: actionable -> Task created + provenance;
            # newsletter -> no Task (keyword pre-filter, AI never
            # consulted for it). ─────────────────────────────────────────
            ai_result = await analyze_pending_sources_for_user(db=db, user=owner, org_id=org.id)
            assert ai_result["tasks_created"] == 1
            assert ai_result["emails_skipped_as_non_task"] == 1

            actionable_row = (await db.execute(select(ImportedEmail).where(ImportedEmail.provider_message_id == actionable_message["id"]))).scalar_one()
            assert actionable_row.processing_status == "task_created"
            newsletter_row = (await db.execute(select(ImportedEmail).where(ImportedEmail.provider_message_id == newsletter_message["id"]))).scalar_one()
            assert newsletter_row.processing_status == "no_action_required"

            task = (await db.execute(select(Task).where(Task.name == "Prepare monthly sales report"))).scalar_one()
            created_task_ids.append(task.id)
            assert task.organization_id == org.id

            provenance = (await db.execute(select(TaskSource).where(TaskSource.task_id == task.id))).scalar_one()
            assert provenance.provider == "google"
            assert provenance.source_type == "gmail_email"
            assert provenance.source_external_id == actionable_message["id"]
            assert provenance.confidence == "high"
            assert provenance.ai_generated is True

            # ── 2 (already asserted above via newsletter_row). ──────────────

            # ── 3. Re-sync (simulating a second manual click) + re-analyze
            # must never create a second Task for the same message. ─────────
            await sync_gmail_data_for_user(db=db, user=owner)
            second_ai_result = await analyze_pending_sources_for_user(db=db, user=owner, org_id=org.id)
            assert second_ai_result["tasks_created"] == 0, "re-syncing the same message must never create a second Task"
            assert second_ai_result["sources_analyzed"] == 0, "an already-processed email must not be re-claimed for analysis"

            all_tasks_named = (await db.execute(select(Task).where(Task.name == "Prepare monthly sales report"))).scalars().all()
            assert len(all_tasks_named) == 1, "exactly one Task must exist after duplicate reprocessing"

        finally:
            if created_task_ids:
                await db.execute(delete(TaskSource).where(TaskSource.task_id.in_(created_task_ids)))
                await db.execute(delete(Task).where(Task.id.in_(created_task_ids)))
            await db.execute(delete(ImportedEmail).where(ImportedEmail.integration_account_id == account.id))
            await db.execute(delete(IntegrationAccount).where(IntegrationAccount.id == account.id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id == org.id))
            await db.execute(delete(User).where(User.id == owner.id))
            await db.commit()

    await engine.dispose()


async def _noop():
    return None


def test_gmail_automation_pipeline(monkeypatch):
    asyncio.run(_scenario(monkeypatch))
