"""Regression tests for the Automation Pipeline's confidence/human-review
gating (Automation Pipeline Audit follow-up — spec's "CONFIDENCE + HUMAN
REVIEW"): the AI extractor already returns a `confidence` field per
extracted task, but nothing previously used it — every extracted item
became a Task regardless of confidence. `app.core.automation_pipeline.
meets_auto_create_bar()` (default: HIGH only) now gates this.

Covers:
  1. HIGH confidence -> Task auto-created, source marked task_created.
  2. MEDIUM confidence -> NO Task created; source marked needs_review.
  3. LOW confidence -> NO Task created; source marked needs_review.
  4. A source with BOTH a high- and a medium-confidence item -> the high
     one becomes a Task, the medium one does not, and the source is
     marked partially_created (never silently dropped).
  5. should_create_tasks=False (no actionable items at all) -> source
     marked no_action_required, distinct from needs_review.

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
from app.services.automation_tasks import analyze_pending_sources_for_user
from app.services.llm.base import LLMProvider, LLMResponse


class _ScriptedProvider(LLMProvider):
    def __init__(self, reply: str):
        self._reply = reply

    async def generate_text(self, **kwargs):
        return LLMResponse(text=self._reply)


def _task_item(title: str, confidence: str) -> dict:
    return {
        "title": title, "description": None,
        "suggested_start_date": None, "suggested_due_date": None,
        "suggested_assignee_name": None, "suggested_assignee_email": None,
        "suggested_project_name": None, "suggested_team_name": None,
        "confidence": confidence,
    }


def _payload(tasks: list[dict], should_create: bool = True) -> str:
    return json.dumps({
        "should_create_tasks": should_create,
        "reason": "clear action item" if should_create else "no action items",
        "source_category": "work_action" if should_create else "internal_update",
        "tasks": tasks,
    })


async def _make_email(db, account_id: int, suffix: str, subject: str) -> ImportedEmail:
    email = ImportedEmail(
        integration_account_id=account_id,
        provider_message_id=f"msg-{suffix}",
        subject=subject,
        sender=f"sender.{suffix}@example.com",
        recipients=[],
        received_at=datetime.now(timezone.utc),
        snippet=f"{subject} — please action this.",
        body_text=f"{subject} — please action this by end of week.",
        raw_payload={},
    )
    db.add(email)
    await db.commit()
    await db.refresh(email)
    return email


async def _scenario(monkeypatch):
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]

        owner = User(full_name="ConfidenceProbe Owner", email=f"confidenceprobe.owner.{suffix}@example-corp.com", hashed_password="x", role=OWNER)
        db.add(owner)
        await db.commit()
        await db.refresh(owner)

        org = Organization(name=f"ConfidenceProbe Org {suffix}", slug=f"confidenceprobe-org-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.commit()
        org = (await db.execute(select(Organization).options(selectinload(Organization.subscription)).where(Organization.id == org.id))).scalar_one()

        db.add(OrganizationMembership(organization_id=org.id, user_id=owner.id, role=OWNER))
        await db.commit()
        owner.last_active_organization_id = org.id
        await db.commit()

        account = IntegrationAccount(
            user_id=owner.id, organization_id=org.id, provider="google",
            account_email=f"confidenceprobe.{suffix}@gmail.com", access_token="fake", refresh_token=None,
        )
        db.add(account)
        await db.commit()
        await db.refresh(account)

        high_email = await _make_email(db, account.id, f"high-{suffix}", f"High confidence task {suffix}")
        medium_email = await _make_email(db, account.id, f"medium-{suffix}", f"Medium confidence task {suffix}")
        low_email = await _make_email(db, account.id, f"low-{suffix}", f"Low confidence task {suffix}")
        mixed_email = await _make_email(db, account.id, f"mixed-{suffix}", f"Mixed confidence task {suffix}")
        no_action_email = await _make_email(db, account.id, f"noaction-{suffix}", f"No action email {suffix}")

        replies_by_message = {
            high_email.provider_message_id: _payload([_task_item(f"High task {suffix}", "high")]),
            medium_email.provider_message_id: _payload([_task_item(f"Medium task {suffix}", "medium")]),
            low_email.provider_message_id: _payload([_task_item(f"Low task {suffix}", "low")]),
            mixed_email.provider_message_id: _payload([
                _task_item(f"Mixed high task {suffix}", "high"),
                _task_item(f"Mixed medium task {suffix}", "medium"),
            ]),
            no_action_email.provider_message_id: _payload([], should_create=False),
        }

        # The extractor doesn't tell us which source it's being called
        # for directly, but source_title is passed straight through into
        # the user_prompt — reuse that to pick the right scripted reply
        # per call, keyed by subject (which is unique per email above).
        subject_to_message_id = {
            high_email.subject: high_email.provider_message_id,
            medium_email.subject: medium_email.provider_message_id,
            low_email.subject: low_email.provider_message_id,
            mixed_email.subject: mixed_email.provider_message_id,
            no_action_email.subject: no_action_email.provider_message_id,
        }

        class _RoutingProvider(LLMProvider):
            async def generate_text(self, *, user_prompt, **kwargs):
                for subject, message_id in subject_to_message_id.items():
                    if subject in user_prompt:
                        return LLMResponse(text=replies_by_message[message_id])
                raise AssertionError(f"no scripted reply for prompt: {user_prompt[:200]}")

        monkeypatch.setattr("app.services.ai_task_extractor.get_llm_provider", lambda: _RoutingProvider())

        created_task_ids: list[int] = []

        try:
            ai_result = await analyze_pending_sources_for_user(db=db, user=owner, org_id=org.id)

            # ── 1. HIGH -> Task auto-created. ────────────────────────────────
            high_task = (await db.execute(select(Task).where(Task.name == f"High task {suffix}"))).scalar_one_or_none()
            assert high_task is not None, "a high-confidence action item must be auto-created as a Task"
            created_task_ids.append(high_task.id)
            high_status = (await db.execute(select(ImportedEmail.processing_status).where(ImportedEmail.id == high_email.id))).scalar_one()
            assert high_status == "task_created"

            # ── 2. MEDIUM -> no Task, needs_review. ──────────────────────────
            medium_task = (await db.execute(select(Task).where(Task.name == f"Medium task {suffix}"))).scalar_one_or_none()
            assert medium_task is None, "a medium-confidence item must NOT be auto-created"
            medium_status = (await db.execute(select(ImportedEmail.processing_status).where(ImportedEmail.id == medium_email.id))).scalar_one()
            assert medium_status == "needs_review"

            # ── 3. LOW -> no Task, needs_review. ─────────────────────────────
            low_task = (await db.execute(select(Task).where(Task.name == f"Low task {suffix}"))).scalar_one_or_none()
            assert low_task is None, "a low-confidence item must NOT be auto-created"
            low_status = (await db.execute(select(ImportedEmail.processing_status).where(ImportedEmail.id == low_email.id))).scalar_one()
            assert low_status == "needs_review"

            # ── 4. Mixed source: high item created, medium item is not,
            # source marked partially_created. ───────────────────────────────
            mixed_high_task = (await db.execute(select(Task).where(Task.name == f"Mixed high task {suffix}"))).scalar_one_or_none()
            assert mixed_high_task is not None
            created_task_ids.append(mixed_high_task.id)
            mixed_medium_task = (await db.execute(select(Task).where(Task.name == f"Mixed medium task {suffix}"))).scalar_one_or_none()
            assert mixed_medium_task is None
            mixed_status = (await db.execute(select(ImportedEmail.processing_status).where(ImportedEmail.id == mixed_email.id))).scalar_one()
            assert mixed_status == "partially_created"

            # ── 5. should_create_tasks=False -> no_action_required,
            # distinct from needs_review. ─────────────────────────────────
            no_action_status = (await db.execute(select(ImportedEmail.processing_status).where(ImportedEmail.id == no_action_email.id))).scalar_one()
            assert no_action_status == "no_action_required"

            assert ai_result["tasks_created"] == 2  # high_task + mixed_high_task
            assert ai_result["tasks_needing_review"] == 3  # medium + low + mixed-medium

            # Provenance recorded confidence accurately.
            high_provenance = (await db.execute(select(TaskSource).where(TaskSource.task_id == high_task.id))).scalar_one()
            assert high_provenance.confidence == "high"

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


def test_automation_confidence_gating(monkeypatch):
    asyncio.run(_scenario(monkeypatch))
