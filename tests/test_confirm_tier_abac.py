"""CONFIRM-tier ABAC regression test (strict acceptance audit security gap
#1 — "CONFIRM-tier Project Manager ABAC bypass").

BUG BEING GUARDED AGAINST: `reassign_task` and `update_task_bulk` both list
PROJECT_MANAGER in their allowed_roles (tools/task_tools.py's
_TASK_WRITE_ROLES), but the ABAC project-membership scoping
(_check_project_scope) was only ever called from the AUTO-tier handlers —
transaction.py's execute_confirmed_change_set(), which is what actually
applies every CONFIRM-tier tool regardless of which chat_service.py handler
built the change set, never checked it at all. A PROJECT_MANAGER could
build a change set to reassign or bulk-update a task in a project they
don't manage and have it go through, purely because it went through the
confirm flow instead of the instant-apply one.

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete, select

from app.core.org_roles import PROJECT_MANAGER
from app.core.database import AsyncSessionLocal, engine
from app.models.chat import ChatSession
from app.models.organization import Organization, OrganizationMembership
from app.models.project import Project
from app.models.task import Task
from app.models.user import User
from app.schemas.task import TaskCreate
from app.services.copilot import change_sets
from app.services.copilot.transaction import execute_confirmed_change_set
from app.repositories.task_repository import TaskRepository


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="ConfirmABAC Owner", email=f"confirmabac.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        pm_out = User(full_name="ConfirmABAC PM Out-of-Scope", email=f"confirmabac.pmout.{suffix}@test.invalid", hashed_password="x", role="project_manager")
        db.add_all([owner, pm_out])
        await db.flush()

        org = Organization(name=f"ConfirmABAC Org {suffix}", slug=f"confirmabac-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.flush()
        db.add_all([
            OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner"),
            OrganizationMembership(organization_id=org.id, user_id=pm_out.id, role="project_manager"),
        ])
        session = ChatSession(user_id=pm_out.id, organization_id=org.id)
        db.add(session)
        await db.commit()
        await db.refresh(session)
        org_id, session_id, pm_out_id = org.id, session.id, pm_out.id

        project = Project(name=f"ConfirmABAC Project {suffix}", created_by_id=owner.id, organization_id=org_id)
        db.add(project)
        await db.flush()
        project_id = project.id
        # Deliberately NOT adding pm_out as a ProjectMembership — they do not manage this project.
        await db.commit()

        task_repo = TaskRepository(db, org_id)
        task = await task_repo.create(TaskCreate(name=f"ConfirmABAC Task {suffix}", project_id=project_id), created_by_id=owner.id)
        task_id = task.id

        try:
            # ── reassign_task: out-of-scope PM's confirm must be refused ──
            reassign_cs = await change_sets.build_change_set(
                db, org_id=org_id, session_id=session_id, user_id=pm_out_id,
                tool_name="reassign_task",
                params={"task_id": task_id, "updates": {"assignee_id": owner.id}},
                affected_tasks=[task], affected_summary=f'"{task.name}"',
            )
            await db.commit()
            result = await execute_confirmed_change_set(
                db, org_id=org_id, org_role=PROJECT_MANAGER, change_set=reassign_cs,
            )
            assert not result.success, "CONFIRM-TIER ABAC BUG: out-of-scope PM's reassign_task confirm must be refused"
            row = (await db.execute(select(Task).where(Task.id == task_id))).scalar_one()
            assert row.assignee_id != owner.id, "CONFIRM-TIER ABAC BUG: reassignment was actually applied despite refusal"

            # ── update_task_bulk: same check, batch path ──
            bulk_cs = await change_sets.build_change_set(
                db, org_id=org_id, session_id=session_id, user_id=pm_out_id,
                tool_name="update_task_bulk",
                params={"task_ids": [task_id], "updates": {"status": "done"}},
                affected_tasks=[task], affected_summary=f'"{task.name}"',
            )
            await db.commit()
            result = await execute_confirmed_change_set(
                db, org_id=org_id, org_role=PROJECT_MANAGER, change_set=bulk_cs,
            )
            assert not result.success, "CONFIRM-TIER ABAC BUG: out-of-scope PM's update_task_bulk confirm must be refused"
            row = (await db.execute(select(Task).where(Task.id == task_id))).scalar_one()
            assert row.status != "done", "CONFIRM-TIER ABAC BUG: bulk update was actually applied despite refusal"

        finally:
            await db.execute(delete(Task).where(Task.id == task_id))
            await db.execute(delete(Project).where(Project.id == project_id))
            await db.execute(delete(ChatSession).where(ChatSession.id == session_id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org_id))
            await db.execute(delete(Organization).where(Organization.id == org_id))
            await db.execute(delete(User).where(User.id.in_([owner.id, pm_out_id])))
            await db.commit()

    await engine.dispose()


def test_confirm_tier_refuses_out_of_scope_project_manager():
    asyncio.run(_scenario())
