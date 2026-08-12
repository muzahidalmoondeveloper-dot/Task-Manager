"""Read-only Domain Tool regression tests (architecture Section 25/26, read
side; master-prompt item 3).

Runs against the real database connection the app uses (AsyncSessionLocal),
same convention as the other live-DB tests in this suite. Every row this
test creates is deleted before it returns, success or failure alike.

Covers: search_rocks / search_issues / search_kpis / search_meetings are
refused for CLIENT (matching the pre-existing CLIENT_BLOCKED_SUB_INTENTS
rule, now expressed once via ToolSpec.client_blocked) and return real,
org-scoped, DB-backed rows for staff roles; search_client_requests is NOT
blocked for CLIENT but scopes results to only the client's own submissions
(ABAC) — a second client's request must never appear in the first client's
result set.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete

from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import CLIENT, TEAM_MANAGER
from app.models.organization import Organization, OrganizationMembership
from app.models.project import Project
from app.models.rock import Rock
from app.models.task_request import TaskRequest
from app.models.team import Team
from app.models.user import User
from app.services.copilot.tools import ToolContext, run_tool


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="ReadTool Owner", email=f"readtool.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        manager = User(full_name="ReadTool Manager", email=f"readtool.mgr.{suffix}@test.invalid", hashed_password="x", role="team_manager")
        client_a = User(full_name="ReadTool Client A", email=f"readtool.clienta.{suffix}@test.invalid", hashed_password="x", role="client")
        client_b = User(full_name="ReadTool Client B", email=f"readtool.clientb.{suffix}@test.invalid", hashed_password="x", role="client")
        db.add_all([owner, manager, client_a, client_b])
        await db.flush()

        org = Organization(name=f"ReadTool Test Org {suffix}", slug=f"readtool-test-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.flush()

        db.add_all([
            OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner"),
            OrganizationMembership(organization_id=org.id, user_id=manager.id, role="team_manager"),
            OrganizationMembership(organization_id=org.id, user_id=client_a.id, role="client"),
            OrganizationMembership(organization_id=org.id, user_id=client_b.id, role="client"),
        ])
        await db.commit()

        team = Team(name=f"ReadTool Team {suffix}", team_manager_id=manager.id, created_by_id=owner.id, organization_id=org.id)
        project = Project(name=f"ReadTool Project {suffix}", created_by_id=owner.id, organization_id=org.id)
        db.add_all([team, project])
        await db.flush()

        rock = Rock(title=f"ReadTool Rock {suffix}", team_id=team.id, organization_id=org.id)
        request_a = TaskRequest(organization_id=org.id, project_id=project.id, submitted_by_id=client_a.id, title=f"Request A {suffix}")
        request_b = TaskRequest(organization_id=org.id, project_id=project.id, submitted_by_id=client_b.id, title=f"Request B {suffix}")
        db.add_all([rock, request_a, request_b])
        await db.commit()

        try:
            mgr_ctx = ToolContext(db=db, org_id=org.id, org_role=TEAM_MANAGER, user=manager, user_id=manager.id, session_id=None)
            client_a_ctx = ToolContext(db=db, org_id=org.id, org_role=CLIENT, user=client_a, user_id=client_a.id, session_id=None)

            # ── staff can search rocks; real DB-backed data comes back ──
            result = await run_tool("search_rocks", {}, mgr_ctx)
            assert result.ok, f"team_manager search_rocks should succeed, got: {result.message}"
            titles = {r.title for r in result.data["items"]}
            assert rock.title in titles, "search_rocks must return the real row just created"

            # ── CLIENT is refused for search_rocks/issues/kpis/meetings/projects/teams ──
            for tool_name in (
                "search_rocks", "search_issues", "search_kpis", "search_meetings",
                "search_projects", "search_teams",
            ):
                result = await run_tool(tool_name, {}, client_a_ctx)
                assert not result.ok, f"CLIENT must be refused for {tool_name}"

            # ── search_projects/search_teams (strict acceptance audit gap
            #    #6 — "Projects/Teams reads bypass the Tool Registry")
            #    return real, org-scoped, DB-backed rows for staff ──
            result = await run_tool("search_projects", {}, mgr_ctx)
            assert result.ok, f"team_manager search_projects should succeed, got: {result.message}"
            names = {p.name for p in result.data["items"]}
            assert project.name in names, "search_projects must return the real row just created"

            result = await run_tool("search_teams", {}, mgr_ctx)
            assert result.ok, f"team_manager search_teams should succeed, got: {result.message}"
            names = {t.name for t in result.data["items"]}
            assert team.name in names, "search_teams must return the real row just created"

            # ── search_client_requests is NOT blocked for CLIENT, but is ABAC-scoped ──
            result = await run_tool("search_client_requests", {}, client_a_ctx)
            assert result.ok, f"CLIENT should be allowed to search their own requests, got: {result.message}"
            titles = {r.title for r in result.data["items"]}
            assert request_a.title in titles, "client A must see their own request"
            assert request_b.title not in titles, "ABAC LEAK: client A can see client B's request"

            # Staff sees both.
            result = await run_tool("search_client_requests", {}, mgr_ctx)
            assert result.ok
            titles = {r.title for r in result.data["items"]}
            assert {request_a.title, request_b.title} <= titles, "staff must see every client's requests"

            # ── Unknown parameter is rejected, not silently ignored ──
            result = await run_tool("search_rocks", {"team_id": 1, "bogus": True}, mgr_ctx)
            assert not result.ok, "unknown parameter to a read tool must be rejected (extra='forbid')"

        finally:
            await db.execute(delete(TaskRequest).where(TaskRequest.id.in_([request_a.id, request_b.id])))
            await db.execute(delete(Rock).where(Rock.id == rock.id))
            await db.execute(delete(Project).where(Project.id == project.id))
            await db.execute(delete(Team).where(Team.id == team.id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id == org.id))
            await db.execute(delete(User).where(User.id.in_([owner.id, manager.id, client_a.id, client_b.id])))
            await db.commit()

    await engine.dispose()


def test_read_tools_enforce_client_visibility_and_abac_scoping():
    asyncio.run(_scenario())
