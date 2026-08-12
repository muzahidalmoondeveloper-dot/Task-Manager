"""Idempotency regression test (architecture item 13 — "Idempotency" for
tool writes).

Runs against the real database connection the app uses (AsyncSessionLocal),
same convention as the other live-DB tests. Every row this test creates is
deleted before it returns, success or failure alike.

Proves: two run_tool("create_task", ...) calls with the identical
session_id + params within the dedup window create exactly ONE task, not
two — simulating a client-side retry (dropped response, double-submit)
resending the same request. A call with DIFFERENT params (a different task
name) in the same session is NOT deduped — it's a genuinely different
request and must still go through. A call with session_id=None (nothing to
scope a dedup key to) is never deduped, matching the documented limitation.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete, select

from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import TEAM_MANAGER
from app.models.chat import ChatSession
from app.models.copilot import AIToolExecution
from app.models.organization import Organization, OrganizationMembership
from app.models.task import Task
from app.models.user import User
from app.services.copilot.tools import ToolContext, run_tool


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="Idempotency Test Owner", email=f"idem.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        db.add(owner)
        await db.flush()

        org = Organization(name=f"Idempotency Test Org {suffix}", slug=f"idem-test-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.flush()
        db.add(OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner"))
        session = ChatSession(user_id=owner.id, organization_id=org.id)
        db.add(session)
        await db.commit()
        await db.refresh(session)

        created_task_ids: list[int] = []
        try:
            ctx = ToolContext(db=db, org_id=org.id, org_role=TEAM_MANAGER, user=owner, user_id=owner.id, session_id=session.id)

            task_name = f"Idempotent task {suffix}"

            # ── Same session + identical params, called twice -> one task only ──
            result1 = await run_tool("create_task", {"tasks": [{"name": task_name}]}, ctx)
            assert result1.ok
            created_task_ids.extend(result1.data["task_ids"])

            result2 = await run_tool("create_task", {"tasks": [{"name": task_name}]}, ctx)
            assert result2.ok, "a retried identical call must still report success (the original outcome), not fail"
            # No new task_ids in result2's data — it's a replayed audit row, not a fresh execution.
            assert not (result2.data or {}).get("task_ids"), "a deduped call must not report freshly-created task_ids"

            matching = (await db.execute(select(Task).where(Task.name == task_name))).scalars().all()
            assert len(matching) == 1, (
                f"IDEMPOTENCY BUG: expected exactly 1 task named {task_name!r} after 2 identical calls, found {len(matching)}"
            )

            # ── Different params (different name), same session -> NOT deduped ──
            other_name = f"Different idempotent task {suffix}"
            result3 = await run_tool("create_task", {"tasks": [{"name": other_name}]}, ctx)
            assert result3.ok
            created_task_ids.extend(result3.data["task_ids"])
            matching_other = (await db.execute(select(Task).where(Task.name == other_name))).scalars().all()
            assert len(matching_other) == 1, "a genuinely different request in the same session must not be deduped away"

            # ── No session_id -> never deduped (two calls -> two tasks) ──
            no_session_ctx = ToolContext(db=db, org_id=org.id, org_role=TEAM_MANAGER, user=owner, user_id=owner.id, session_id=None)
            no_session_name = f"No-session task {suffix}"
            r_a = await run_tool("create_task", {"tasks": [{"name": no_session_name}]}, no_session_ctx)
            r_b = await run_tool("create_task", {"tasks": [{"name": no_session_name}]}, no_session_ctx)
            created_task_ids.extend(r_a.data["task_ids"])
            created_task_ids.extend(r_b.data["task_ids"])
            matching_no_session = (await db.execute(select(Task).where(Task.name == no_session_name))).scalars().all()
            assert len(matching_no_session) == 2, "without a session_id there's no dedup scope, so both calls must execute"

        finally:
            if created_task_ids:
                await db.execute(delete(Task).where(Task.id.in_(created_task_ids)))
            await db.execute(delete(AIToolExecution).where(AIToolExecution.organization_id == org.id))
            await db.execute(delete(ChatSession).where(ChatSession.id == session.id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id == org.id))
            await db.execute(delete(User).where(User.id == owner.id))
            await db.commit()

    await engine.dispose()


def test_create_task_idempotency_deduplicates_identical_retries():
    asyncio.run(_scenario())
