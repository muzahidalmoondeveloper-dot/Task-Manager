"""Partial-failure/compensation handling regression test (architecture item
16) for the AUTO-tier create_task tool.

BUG BEING GUARDED AGAINST: task_tools.py's _create_task_handler used to call
TaskRepository.create() per item in a multi-task batch — and that method
commits internally. A batch of [valid_task, invalid_task] would create AND
PERMANENTLY COMMIT the valid task before ever reaching the invalid one,
whose failure would then raise, trigger run_tool()'s db.rollback() (which
only undoes *uncommitted* work — the valid task was already committed and
gone), and report "Something went wrong applying that. Nothing was
changed." to the user — false; one task silently existed anyway.

The fix builds every task in the batch via the ORM + a single flush() (never
committing inside the handler), so the whole batch lives in one uncommitted
transaction; run_tool()'s rollback (on either an exception OR a soft
ToolResult(False, ...) failure — both paths are covered here) now undoes
every row in the batch atomically, not just the one that failed.

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete, select

from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import PROJECT_MANAGER, TEAM_MANAGER
from app.models.organization import Organization, OrganizationMembership
from app.models.project import Project
from app.models.task import Task
from app.models.user import User
from app.services.copilot.tools import ToolContext, run_tool


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="Atomicity Test Owner", email=f"atomicity.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        manager = User(full_name="Atomicity Test Manager", email=f"atomicity.mgr.{suffix}@test.invalid", hashed_password="x", role="team_manager")
        pm = User(full_name="Atomicity Test PM", email=f"atomicity.pm.{suffix}@test.invalid", hashed_password="x", role="project_manager")
        db.add_all([owner, manager, pm])
        await db.flush()

        org = Organization(name=f"Atomicity Test Org {suffix}", slug=f"atomicity-test-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.flush()
        db.add_all([
            OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner"),
            OrganizationMembership(organization_id=org.id, user_id=manager.id, role="team_manager"),
            OrganizationMembership(organization_id=org.id, user_id=pm.id, role="project_manager"),
        ])
        await db.commit()
        org_id, manager_id, pm_id, owner_id = org.id, manager.id, pm.id, owner.id

        try:
            # ── 1. Exception-path atomicity: a batch where item 2 violates a
            #    DB constraint (nonexistent project_id FK) must roll back
            #    item 1 too — neither task may exist afterward. ──
            mgr_ctx = ToolContext(db=db, org_id=org_id, org_role=TEAM_MANAGER, user=manager, user_id=manager_id, session_id=None)
            valid_name = f"Atomicity valid task {suffix}"
            invalid_name = f"Atomicity invalid task {suffix}"
            result = await run_tool(
                "create_task",
                {"tasks": [
                    {"name": valid_name},
                    {"name": invalid_name, "project_id": 2_147_483_647},  # does not exist -> FK violation on flush
                ]},
                mgr_ctx,
            )
            assert not result.ok, "a batch with an invalid item must fail overall"

            leaked_valid = (await db.execute(select(Task).where(Task.name == valid_name))).scalar_one_or_none()
            assert leaked_valid is None, (
                "ATOMICITY BUG: the valid task in a failed batch was left committed anyway"
            )
            leaked_invalid = (await db.execute(select(Task).where(Task.name == invalid_name))).scalar_one_or_none()
            assert leaked_invalid is None

            # The session must still be usable after the rollback+re-raise
            # (a botched recovery could leave the session broken for the
            # rest of the request).
            result = await run_tool("create_task", {"tasks": [{"name": f"Post-failure sanity task {suffix}"}]}, mgr_ctx)
            assert result.ok, f"session must remain usable after a prior batch failure, got: {result.message}"
            sanity_task_id = result.data["task_ids"][0]

            # ── 2. Soft-failure (ABAC refusal) path atomicity: a batch
            #    where a LATER item trips a project-scope refusal must not
            #    leave an EARLIER item's row committed either. ──
            project = Project(name=f"Atomicity Test Project {suffix}", created_by_id=owner_id, organization_id=org_id)
            db.add(project)
            await db.flush()
            project_id = project.id
            await db.commit()

            pm_ctx = ToolContext(db=db, org_id=org_id, org_role=PROJECT_MANAGER, user=pm, user_id=pm_id, session_id=None)
            in_scope_name = f"Atomicity PM in-scope task {suffix}"
            out_of_scope_name = f"Atomicity PM out-of-scope task {suffix}"
            result = await run_tool(
                "create_task",
                {"tasks": [
                    {"name": in_scope_name, "project_id": project_id},  # pm has no membership -> scope refusal
                    {"name": out_of_scope_name},
                ]},
                pm_ctx,
            )
            assert not result.ok, "an out-of-scope item anywhere in the batch must fail the whole batch"
            leaked = (await db.execute(select(Task).where(Task.name.in_([in_scope_name, out_of_scope_name])))).scalars().all()
            assert not leaked, "ATOMICITY BUG: a soft (non-exception) failure left part of the batch committed"

            await db.execute(delete(Project).where(Project.id == project_id))
            await db.commit()

        finally:
            await db.execute(delete(Task).where(Task.name.in_([
                valid_name, invalid_name, f"Post-failure sanity task {suffix}",
                f"Atomicity PM in-scope task {suffix}", f"Atomicity PM out-of-scope task {suffix}",
            ])))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org_id))
            await db.execute(delete(Organization).where(Organization.id == org_id))
            await db.execute(delete(User).where(User.id.in_([owner_id, manager_id, pm_id])))
            await db.commit()

    await engine.dispose()


def test_create_task_batch_failure_is_fully_atomic():
    asyncio.run(_scenario())
