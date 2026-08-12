"""Domain Tool Registry regression tests (architecture Section 4.5 / 25 / 26).

Runs against the real database connection the app uses (AsyncSessionLocal) —
same convention as the other live-DB tests in this suite, since this repo has
no isolated test-DB/transaction-rollback fixture. Every row this test creates
is deleted before it returns, success or failure alike.

BUG BEING GUARDED AGAINST: ChatService._handle_create_task() (and, it turned
out, _handle_analyze_text()'s inline task creation) used to execute directly
against TaskRepository with NO organization-role parameter and NO
authorization check of any kind — a CLIENT-role user could create/update
real tasks through chat, directly contradicting policy.py's own documented
rule ("Clients have no write tools available through chat at all today").
This test exercises the Domain Tool Registry's run_tool()/
check_write_authorized() directly (the same entry points chat_service.py's
handlers now call) and proves:
  1. An authorized role (team_manager) can create a task through run_tool(),
     and the created row is verifiable afterward (post-write verification).
  2. A CLIENT-role user is refused by run_tool() for create_task — nothing
     is written to the database.
  3. check_write_authorized() refuses CLIENT for every task write tool
     (create_task, update_task_field, reassign_task, delete_task_single,
     update_task_bulk, convert_client_request_to_task) and allows
     team_manager for the same set (except the destructive-only subset,
     which team_manager is still allowed for per the existing product rule).
  4. Schema validation in run_tool() rejects an unknown/malformed parameter
     instead of silently ignoring it.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete

from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import CLIENT, PROJECT_MANAGER, TEAM_MANAGER
from app.models.organization import Organization, OrganizationMembership
from app.models.project import Project, ProjectMembership
from app.models.task import Task
from app.models.user import User
from app.services.copilot.tools import ToolContext, check_write_authorized, run_tool
from app.services.copilot.tools.registry import TOOL_REGISTRY


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="Registry Test Owner", email=f"registry.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        manager = User(full_name="Registry Test Manager", email=f"registry.mgr.{suffix}@test.invalid", hashed_password="x", role="team_manager")
        client_user = User(full_name="Registry Test Client", email=f"registry.client.{suffix}@test.invalid", hashed_password="x", role="client")
        db.add_all([owner, manager, client_user])
        await db.flush()

        org = Organization(name=f"Tool Registry Test Org {suffix}", slug=f"tool-registry-test-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.flush()

        pm_in = User(full_name="Registry Test PM In-Scope", email=f"registry.pm.in.{suffix}@test.invalid", hashed_password="x", role="project_manager")
        pm_out = User(full_name="Registry Test PM Out-of-Scope", email=f"registry.pm.out.{suffix}@test.invalid", hashed_password="x", role="project_manager")
        db.add_all([pm_in, pm_out])
        await db.flush()

        db.add_all([
            OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner"),
            OrganizationMembership(organization_id=org.id, user_id=manager.id, role="team_manager"),
            OrganizationMembership(organization_id=org.id, user_id=client_user.id, role="client"),
            OrganizationMembership(organization_id=org.id, user_id=pm_in.id, role="project_manager"),
            OrganizationMembership(organization_id=org.id, user_id=pm_out.id, role="project_manager"),
        ])
        await db.commit()

        project = Project(name=f"Registry Test Project {suffix}", created_by_id=owner.id, organization_id=org.id)
        db.add(project)
        await db.flush()
        db.add(ProjectMembership(project_id=project.id, user_id=pm_in.id))
        await db.commit()

        # Captured as plain locals up front — a soft tool failure inside the
        # try block below now correctly triggers a db.rollback() (the exact
        # partial-failure fix this phase added), which expires every ORM
        # attribute on every object still attached to this session. Reading
        # e.g. project.id in the `finally` cleanup afterward would trigger
        # an implicit (illegal, in async mode) reload — same pitfall as
        # transaction.py's execute_confirmed_change_set, same fix.
        org_id, project_id = org.id, project.id
        owner_id, manager_id, client_user_id = owner.id, manager.id, client_user.id
        pm_in_id, pm_out_id = pm_in.id, pm_out.id

        created_task_ids: list[int] = []
        try:
            # ── 1. Authorized role (team_manager) can create via run_tool(), verified post-write ──
            mgr_ctx = ToolContext(db=db, org_id=org_id, org_role=TEAM_MANAGER, user=manager, user_id=manager_id, session_id=None)
            task_name = f"Registry test task {suffix}"
            result = await run_tool("create_task", {"tasks": [{"name": task_name}]}, mgr_ctx)
            assert result.ok, f"team_manager create_task should succeed, got: {result.message}"
            assert result.data and result.data.get("task_ids"), "create_task result must report created task_ids"
            created_task_ids.extend(result.data["task_ids"])

            from sqlalchemy import select
            row = (await db.execute(select(Task).where(Task.id == created_task_ids[0]))).scalar_one_or_none()
            assert row is not None and row.name == task_name, "created task must actually exist with the expected name"

            # ── 2. CLIENT is refused by run_tool() for create_task — nothing written ──
            client_ctx = ToolContext(db=db, org_id=org_id, org_role=CLIENT, user=client_user, user_id=client_user_id, session_id=None)
            before_count = len(created_task_ids)
            result = await run_tool("create_task", {"tasks": [{"name": f"Should never exist {suffix}"}]}, client_ctx)
            assert not result.ok, "CLIENT must be refused for create_task"
            assert "permission" in result.message.lower(), f"refusal message should mention permission, got: {result.message!r}"

            leaked = (await db.execute(
                select(Task).where(Task.name == f"Should never exist {suffix}")
            )).scalar_one_or_none()
            assert leaked is None, "CROSS-ROLE LEAK: CLIENT-role create_task call actually wrote a row"

            # ── 3. check_write_authorized() — CLIENT denied, team_manager allowed, for every registered task tool ──
            task_tool_names = [
                "create_task", "update_task_field", "reassign_task",
                "delete_task_single", "update_task_bulk", "convert_client_request_to_task",
            ]
            for tool_name in task_tool_names:
                assert tool_name in TOOL_REGISTRY, f"{tool_name} must be registered"
                refusal = check_write_authorized(org_role=CLIENT, tool_name=tool_name)
                assert refusal is not None, f"CLIENT must be refused for {tool_name}"
                refusal = check_write_authorized(org_role=TEAM_MANAGER, tool_name=tool_name)
                assert refusal is None, f"team_manager must be allowed for {tool_name}, got: {refusal!r}"

            # ── 4. Schema validation rejects malformed/unknown params ──
            result = await run_tool("create_task", {"tasks": []}, mgr_ctx)
            assert not result.ok, "empty tasks list must fail schema validation (min_length=1)"

            result = await run_tool("create_task", {"tasks": [{"name": "x", "not_a_real_field": 123}]}, mgr_ctx)
            assert not result.ok, "unknown parameter must be rejected (extra='forbid'), not silently ignored"

            result = await run_tool("update_task_field", {"task_id": created_task_ids[0], "name": "Renamed via registry"}, mgr_ctx)
            assert result.ok, f"update_task_field should succeed for team_manager, got: {result.message}"
            row = (await db.execute(select(Task).where(Task.id == created_task_ids[0]))).scalar_one_or_none()
            assert row is not None and row.name == "Renamed via registry", "update_task_field must persist and verify"

            # ── 5. ABAC (master-prompt item 2): PROJECT_MANAGER is scoped to
            #    projects they're actually a member of — RBAC alone (role
            #    name only) is not sufficient here. ──
            pm_in_ctx = ToolContext(db=db, org_id=org_id, org_role=PROJECT_MANAGER, user=pm_in, user_id=pm_in_id, session_id=None)
            pm_out_ctx = ToolContext(db=db, org_id=org_id, org_role=PROJECT_MANAGER, user=pm_out, user_id=pm_out_id, session_id=None)

            in_scope_name = f"PM in-scope task {suffix}"
            result = await run_tool("create_task", {"tasks": [{"name": in_scope_name, "project_id": project_id}]}, pm_in_ctx)
            assert result.ok, f"in-scope project manager should be able to create a task in their own project, got: {result.message}"
            created_task_ids.extend(result.data["task_ids"])

            out_of_scope_name = f"PM out-of-scope task {suffix}"
            result = await run_tool("create_task", {"tasks": [{"name": out_of_scope_name, "project_id": project_id}]}, pm_out_ctx)
            assert not result.ok, "ABAC LEAK: out-of-scope project manager was able to create a task in a project they don't manage"
            leaked = (await db.execute(select(Task).where(Task.name == out_of_scope_name))).scalar_one_or_none()
            assert leaked is None, "ABAC LEAK: out-of-scope project manager's create_task call actually wrote a row"

            result = await run_tool(
                "update_task_field",
                {"task_id": created_task_ids[-1], "name": "PM out-of-scope should not rename this"},
                pm_out_ctx,
            )
            assert not result.ok, "ABAC LEAK: out-of-scope project manager was able to update a task in a project they don't manage"

        finally:
            if created_task_ids:
                await db.execute(delete(Task).where(Task.id.in_(created_task_ids)))
            await db.execute(delete(ProjectMembership).where(ProjectMembership.project_id == project_id))
            await db.execute(delete(Project).where(Project.id == project_id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org_id))
            await db.execute(delete(Organization).where(Organization.id == org_id))
            await db.execute(delete(User).where(User.id.in_([owner_id, manager_id, client_user_id, pm_in_id, pm_out_id])))
            await db.commit()

    # See test_chat_service_safety.py's matching comment — disposing here,
    # on this same event loop, prevents a pooled connection from this loop
    # being reused by a later independent asyncio.run() in another test file.
    await engine.dispose()


def test_tool_registry_enforces_authorization_and_verification():
    asyncio.run(_scenario())
