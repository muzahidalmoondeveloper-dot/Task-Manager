"""Domain buildout regression tests — Issues, Rocks, KPI, Client Requests
write tools (continuing the strict acceptance audit's priority list: these
domains were READ_ONLY or NOT_IMPLEMENTED for writes).

Each tool is exercised through the exact same run_tool() entry point
chat_service.py's handlers use, proving the full path: schema validation →
RBAC → ABAC (project scope, where applicable) → execute → post-write
verification → audit. NOTE (explicitly, per the strict acceptance audit's
own methodology — "do not count files/classes as implemented unless wired
into chatbot runtime"): these tools are registered in TOOL_REGISTRY and
fully functional via run_tool(), but chat_service.py does not yet route any
db_query/intent sub-case to them — there is no natural-language entry point
("create an issue called X") that reaches them yet. That wiring is tracked
separately; this file proves the tools themselves are correct in isolation,
the same way test_tool_registry.py did for the original task tools before
chat_service.py's handlers were built on top of them.

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.
"""

import asyncio
import uuid
from datetime import date

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete, select

from app.core.org_roles import CLIENT, TEAM_MANAGER
from app.core.database import AsyncSessionLocal, engine
from app.models.issue import Issue
from app.models.kpi import KPI, KPIEntry
from app.models.organization import Organization, OrganizationMembership
from app.models.project import Project, ProjectMembership
from app.models.rock import Rock
from app.models.task_request import TaskRequest
from app.models.team import Team
from app.models.user import User
from app.services.copilot.tools import ToolContext, run_tool


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="Buildout Owner", email=f"buildout.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        manager = User(full_name="Buildout Manager", email=f"buildout.mgr.{suffix}@test.invalid", hashed_password="x", role="team_manager")
        client = User(full_name="Buildout Client", email=f"buildout.client.{suffix}@test.invalid", hashed_password="x", role="client")
        db.add_all([owner, manager, client])
        await db.flush()

        org = Organization(name=f"Buildout Org {suffix}", slug=f"buildout-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.flush()
        db.add_all([
            OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner"),
            OrganizationMembership(organization_id=org.id, user_id=manager.id, role="team_manager"),
            OrganizationMembership(organization_id=org.id, user_id=client.id, role="client"),
        ])
        await db.commit()
        # Captured as plain locals up front — an ABAC/RBAC soft refusal
        # anywhere below correctly triggers run_tool()'s db.rollback() (the
        # partial-failure fix from an earlier phase), which expires every
        # ORM object still attached to this session, including `owner`.
        # Reading owner.id afterward would crash with MissingGreenlet — see
        # tools/registry.py's ToolContext.user_id docstring for the same
        # pitfall in production code.
        org_id, owner_id, manager_id, client_id = org.id, owner.id, manager.id, client.id

        team = Team(name=f"Buildout Team {suffix}", team_manager_id=manager.id, created_by_id=owner.id, organization_id=org_id)
        project = Project(name=f"Buildout Project {suffix}", created_by_id=owner.id, organization_id=org_id)
        db.add_all([team, project])
        await db.flush()
        db.add(ProjectMembership(project_id=project.id, user_id=client.id))
        await db.commit()
        team_id, project_id = team.id, project.id

        created_issue_ids: list[int] = []
        created_rock_ids: list[int] = []
        created_kpi_entry_ids: list[int] = []
        created_request_ids: list[int] = []
        kpi = KPI(title=f"Buildout KPI {suffix}", team_id=team_id, organization_id=org_id)
        db.add(kpi)
        await db.commit()
        kpi_id = kpi.id

        try:
            mgr_ctx = ToolContext(db=db, org_id=org_id, org_role=TEAM_MANAGER, user=manager, user_id=manager_id, session_id=None)
            client_ctx = ToolContext(db=db, org_id=org_id, org_role=CLIENT, user=client, user_id=client_id, session_id=None)

            # ── create_issue ──
            result = await run_tool("create_issue", {"title": f"Buildout Issue {suffix}", "team_id": team_id}, mgr_ctx)
            assert result.ok, f"create_issue should succeed for team_manager, got: {result.message}"
            issue_id = result.data["issue_id"]
            created_issue_ids.append(issue_id)
            row = (await db.execute(select(Issue).where(Issue.id == issue_id))).scalar_one()
            assert row.title == f"Buildout Issue {suffix}" and row.status == "open"

            # ── update_issue_status ──
            result = await run_tool("update_issue_status", {"issue_id": issue_id, "status": "resolved"}, mgr_ctx)
            assert result.ok, f"update_issue_status should succeed, got: {result.message}"
            row = (await db.execute(select(Issue).where(Issue.id == issue_id))).scalar_one()
            assert row.status == "resolved" and row.resolved_at is not None

            # ── CLIENT is refused for issue tools (RBAC) ──
            result = await run_tool("create_issue", {"title": "Should never exist", "team_id": team_id}, client_ctx)
            assert not result.ok, "CLIENT must be refused for create_issue"

            # ── create_rock / update_rock_status ──
            result = await run_tool("create_rock", {"title": f"Buildout Rock {suffix}", "team_id": team_id}, mgr_ctx)
            assert result.ok, f"create_rock should succeed, got: {result.message}"
            rock_id = result.data["rock_id"]
            created_rock_ids.append(rock_id)
            result = await run_tool("update_rock_status", {"rock_id": rock_id, "status": "on_track"}, mgr_ctx)
            assert result.ok
            row = (await db.execute(select(Rock).where(Rock.id == rock_id))).scalar_one()
            assert row.status == "on_track"

            result = await run_tool("update_rock_status", {"rock_id": rock_id, "status": "not_a_real_status"}, mgr_ctx)
            assert not result.ok, "an invalid rock status must be rejected, not silently accepted"

            # ── record_kpi_value (create, then update the same period) ──
            result = await run_tool(
                "record_kpi_value",
                {"kpi_id": kpi_id, "value": 42.0, "period_type": "weekly", "period_start": date(2026, 1, 5).isoformat()},
                mgr_ctx,
            )
            assert result.ok, f"record_kpi_value should succeed, got: {result.message}"
            entry_id = result.data["entry_id"]
            created_kpi_entry_ids.append(entry_id)
            row = (await db.execute(select(KPIEntry).where(KPIEntry.id == entry_id))).scalar_one()
            assert row.value == 42.0

            # Recording the SAME period again must UPDATE, not duplicate.
            result = await run_tool(
                "record_kpi_value",
                {"kpi_id": kpi_id, "value": 55.0, "period_type": "weekly", "period_start": date(2026, 1, 5).isoformat()},
                mgr_ctx,
            )
            assert result.ok
            assert result.data["entry_id"] == entry_id, "recording the same period again must update the existing entry, not create a duplicate"
            row = (await db.execute(select(KPIEntry).where(KPIEntry.id == entry_id))).scalar_one()
            assert row.value == 55.0

            # ── submit_client_request (CLIENT-only) ──
            result = await run_tool(
                "submit_client_request",
                {"title": f"Buildout Client Request {suffix}", "project_id": project_id},
                client_ctx,
            )
            assert result.ok, f"submit_client_request should succeed for CLIENT, got: {result.message}"
            request_id = result.data["request_id"]
            created_request_ids.append(request_id)
            row = (await db.execute(select(TaskRequest).where(TaskRequest.id == request_id))).scalar_one()
            assert row.submitted_by_id == client_id and row.status == "pending"

            # Staff cannot use submit_client_request (RBAC — clients submit, staff create tasks directly).
            result = await run_tool("submit_client_request", {"title": "Staff should not do this", "project_id": project_id}, mgr_ctx)
            assert not result.ok, "team_manager must be refused for submit_client_request"

            # A client cannot submit for a project they're not a member of (ABAC).
            other_project = Project(name=f"Buildout Other Project {suffix}", created_by_id=owner_id, organization_id=org_id)
            db.add(other_project)
            await db.flush()
            other_project_id = other_project.id
            await db.commit()
            result = await run_tool("submit_client_request", {"title": "Should be refused", "project_id": other_project_id}, client_ctx)
            assert not result.ok, "ABAC LEAK: client submitted a request for a project they're not a member of"
            leaked = (await db.execute(select(TaskRequest).where(TaskRequest.title == "Should be refused"))).scalar_one_or_none()
            assert leaked is None

            await db.execute(delete(Project).where(Project.id == other_project_id))
            await db.commit()

        finally:
            if created_issue_ids:
                await db.execute(delete(Issue).where(Issue.id.in_(created_issue_ids)))
            if created_rock_ids:
                await db.execute(delete(Rock).where(Rock.id.in_(created_rock_ids)))
            if created_kpi_entry_ids:
                await db.execute(delete(KPIEntry).where(KPIEntry.id.in_(created_kpi_entry_ids)))
            if created_request_ids:
                await db.execute(delete(TaskRequest).where(TaskRequest.id.in_(created_request_ids)))
            await db.execute(delete(KPI).where(KPI.id == kpi_id))
            await db.execute(delete(ProjectMembership).where(ProjectMembership.project_id == project_id))
            await db.execute(delete(Project).where(Project.id == project_id))
            await db.execute(delete(Team).where(Team.id == team_id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org_id))
            await db.execute(delete(Organization).where(Organization.id == org_id))
            await db.execute(delete(User).where(User.id.in_([owner_id, manager_id, client_id])))
            await db.commit()

    await engine.dispose()


def test_domain_buildout_write_tools_full_path():
    asyncio.run(_scenario())
