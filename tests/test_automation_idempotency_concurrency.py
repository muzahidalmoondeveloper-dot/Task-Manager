"""Regression test for the Automation Pipeline's idempotency/concurrency
protection (Automation Pipeline Audit follow-up).

BUG BEING GUARDED AGAINST: before this follow-up, "already processed" was
tracked with a plain boolean (`ImportedEmail.tasks_extracted`) read then
written at the END of each source's processing loop — a manual sync
overlapping a scheduled sync, two app instances, or a retried request
could all read `tasks_extracted=False` for the SAME email before either
finished, and both would independently run AI extraction and create a
Task, producing duplicates.

FIX: `app.services.automation_tasks._claim_source_rows` atomically claims
pending rows with `UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP
LOCKED) RETURNING` — two concurrent callers can never claim the same row.

This test drives two REAL, independent database sessions racing via
`asyncio.gather` to call `analyze_pending_sources_for_user` for the SAME
user/pending email at the same moment (mirrors
test_change_set_toctou.py's established concurrency-testing pattern),
and asserts exactly one of them processes it and exactly one Task is
created — never zero, never two.

Runs against the real database connection the app uses (AsyncSessionLocal/
asyncpg — SELECT ... FOR UPDATE SKIP LOCKED needs a real transactional
backend). Every row this test creates is deleted before it returns.
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
from app.services.automation_tasks import analyze_pending_sources_for_user
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
            "title": "Concurrency probe task",
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


async def _analyze_via_own_session(org_id, user_id) -> dict:
    """One concurrent participant: its own session/connection, exactly
    like two real concurrent sync requests would each get their own DB
    connection — mirrors test_change_set_toctou.py's own helper."""
    async with AsyncSessionLocal() as db:
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
        # Each session needs its own LLM provider mock — module-level
        # monkeypatch already applies process-wide, so this is a no-op
        # re-affirmation for clarity/isolation, not strictly required.
        return await analyze_pending_sources_for_user(db=db, user=user, org_id=org_id)


async def _scenario(monkeypatch):
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]

        owner = User(full_name="ConcurrencyProbe Owner", email=f"concurrencyprobe.owner.{suffix}@example-corp.com", hashed_password="x", role=OWNER)
        db.add(owner)
        await db.commit()
        await db.refresh(owner)

        org = Organization(name=f"ConcurrencyProbe Org {suffix}", slug=f"concurrencyprobe-org-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.commit()
        org = (await db.execute(select(Organization).options(selectinload(Organization.subscription)).where(Organization.id == org.id))).scalar_one()

        db.add(OrganizationMembership(organization_id=org.id, user_id=owner.id, role=OWNER))
        await db.commit()
        owner.last_active_organization_id = org.id
        await db.commit()

        account = IntegrationAccount(
            user_id=owner.id, organization_id=org.id, provider="google",
            account_email=f"concurrencyprobe.{suffix}@gmail.com", access_token="fake-token", refresh_token=None,
        )
        db.add(account)
        await db.commit()
        await db.refresh(account)

        email = ImportedEmail(
            integration_account_id=account.id,
            provider_message_id=f"msg-{suffix}",
            subject="Please handle the concurrency probe task",
            sender=f"sender.{suffix}@example.com",
            recipients=[],
            received_at=datetime.now(timezone.utc),
            snippet="Please handle the concurrency probe task by tomorrow.",
            body_text="Please handle the concurrency probe task by tomorrow.",
            raw_payload={},
        )
        db.add(email)
        await db.commit()
        await db.refresh(email)

        monkeypatch.setattr(
            "app.services.ai_task_extractor.get_llm_provider",
            lambda: _ScriptedProvider(_payload()),
        )

        created_task_ids: list[int] = []

        try:
            # Two independent sessions race to analyze the SAME pending
            # email at the same moment.
            results = await asyncio.gather(
                _analyze_via_own_session(org.id, owner.id),
                _analyze_via_own_session(org.id, owner.id),
            )

            total_claimed = sum(r["sources_analyzed"] for r in results)
            total_created = sum(r["tasks_created"] for r in results)
            assert total_claimed == 1, f"exactly one of the two concurrent calls must claim the pending email, got {total_claimed}"
            assert total_created == 1, f"exactly one Task must be created, got {total_created}"

            # Column-only select (not `select(Task)`) avoids this
            # session's ORM identity map returning a stale pre-race
            # instance — same reasoning as test_change_set_toctou.py.
            task_ids = (await db.execute(select(Task.id).where(Task.name == "Concurrency probe task"))).scalars().all()
            assert len(task_ids) == 1, "exactly one Task row must exist in the database after the race"
            created_task_ids.extend(task_ids)

            final_status = (await db.execute(select(ImportedEmail.processing_status).where(ImportedEmail.id == email.id))).scalar_one()
            assert final_status == "task_created"

        finally:
            if created_task_ids:
                await db.execute(delete(TaskSource).where(TaskSource.task_id.in_(created_task_ids)))
                await db.execute(delete(Task).where(Task.id.in_(created_task_ids)))
            await db.execute(delete(ImportedEmail).where(ImportedEmail.id == email.id))
            await db.execute(delete(IntegrationAccount).where(IntegrationAccount.id == account.id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id == org.id))
            await db.execute(delete(User).where(User.id == owner.id))
            await db.commit()

    await engine.dispose()


def test_automation_analysis_is_idempotent_under_concurrency(monkeypatch):
    asyncio.run(_scenario(monkeypatch))
