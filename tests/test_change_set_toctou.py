"""Change-set TOCTOU race regression test (strict acceptance audit security
gap #2).

BUG BEING GUARDED AGAINST: the confirm endpoint read a change set's status
with a plain SELECT, checked `status == "pending"`, then executed it. Two
concurrent confirm requests for the SAME change set (double-click, a
client retry after a dropped response) could both read "pending" before
either committed, and both would proceed to apply the change — e.g.
double-reassigning a task or running a bulk update twice.

This test drives two REAL, independent database sessions (mirroring two
concurrent HTTP requests, each with its own connection) racing via
`asyncio.gather` to confirm the identical change set, and asserts:
exactly one succeeds, the other is correctly refused as "already executed"
(not a crash, not a silent double-apply), and the underlying task was only
actually mutated once.

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete, select

from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import TEAM_MANAGER
from app.models.chat import ChatSession
from app.models.organization import Organization, OrganizationMembership
from app.models.task import Task
from app.models.user import User
from app.repositories.task_repository import TaskRepository
from app.schemas.task import TaskCreate
from app.services.copilot import change_sets
from app.services.copilot.transaction import execute_confirmed_change_set


async def _confirm_via_own_session(org_id, change_set_id: int) -> bool:
    """Simulates one concurrent HTTP request: its own DB session, its own
    row-locked fetch, its own execute call — exactly what the real
    confirm_change_set route does."""
    async with AsyncSessionLocal() as db:
        change_set = await change_sets.get_change_set_for_update(db, org_id, change_set_id)
        if change_set is None or change_set.status != "pending":
            return False
        result = await execute_confirmed_change_set(db, org_id=org_id, org_role=TEAM_MANAGER, change_set=change_set)
        return result.success


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="TOCTOU Owner", email=f"toctou.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        target = User(full_name="TOCTOU Target", email=f"toctou.target.{suffix}@test.invalid", hashed_password="x", role="team_member")
        db.add_all([owner, target])
        await db.flush()

        org = Organization(name=f"TOCTOU Org {suffix}", slug=f"toctou-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.flush()
        db.add_all([
            OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner"),
            OrganizationMembership(organization_id=org.id, user_id=target.id, role="team_member"),
        ])
        session = ChatSession(user_id=owner.id, organization_id=org.id)
        db.add(session)
        await db.commit()
        await db.refresh(session)
        org_id, session_id, target_id = org.id, session.id, target.id

        task_repo = TaskRepository(db, org_id)
        task = await task_repo.create(TaskCreate(name=f"TOCTOU Task {suffix}"), created_by_id=owner.id)
        task_id = task.id

        try:
            change_set = await change_sets.build_change_set(
                db, org_id=org_id, session_id=session_id, user_id=owner.id,
                tool_name="reassign_task",
                params={"task_id": task_id, "updates": {"assignee_id": target_id}},
                affected_tasks=[task], affected_summary=f'"{task.name}"',
            )
            await db.commit()
            change_set_id = change_set.id

            # Two independent sessions race to confirm the SAME change set —
            # mirrors two real concurrent HTTP requests.
            results = await asyncio.gather(
                _confirm_via_own_session(org_id, change_set_id),
                _confirm_via_own_session(org_id, change_set_id),
            )

            assert sorted(results) == [False, True], (
                f"TOCTOU BUG: exactly one of two concurrent confirms must succeed, got: {results}"
            )

            # A column-only select (not `select(Task)`) deliberately avoids
            # the ORM identity map — this session's map still holds the
            # `task` object as it was before either concurrent confirm ran
            # (expire_on_commit is False app-wide), and re-selecting the
            # full entity would just hand back that stale cached instance,
            # trivially "passing" without proving what actually got
            # committed to the database by the two racing sessions.
            committed_assignee_id = (
                await db.execute(select(Task.assignee_id).where(Task.id == task_id))
            ).scalar_one()
            assert committed_assignee_id == target_id, "the reassignment must have actually applied exactly once"

        finally:
            await db.execute(delete(Task).where(Task.id == task_id))
            await db.execute(delete(ChatSession).where(ChatSession.id == session_id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org_id))
            await db.execute(delete(Organization).where(Organization.id == org_id))
            await db.execute(delete(User).where(User.id.in_([owner.id, target_id])))
            await db.commit()

    await engine.dispose()


def test_concurrent_confirms_of_the_same_change_set_apply_exactly_once():
    asyncio.run(_scenario())
