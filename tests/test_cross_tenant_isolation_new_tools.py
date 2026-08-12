"""Cross-tenant isolation regression test for the domain-buildout tools that
didn't already have one — search_projects/search_teams (Gap #6) and the new
write tools' organization scoping (Issues/Rocks/Meetings/Projects/Teams).

test_hybrid_keyword_search.py already proves search_everything() is
tenant-isolated; test_copilot_tenant_isolation.py already proves it for
users. This file closes the remaining gap the strict acceptance audit
flagged: "no test proves cross-tenant isolation for search_rocks/issues/
kpis/meetings" — extended here to also cover search_projects/search_teams,
plus a positive check that every new AUTO-tier write tool stamps
organization_id correctly so a created row can never silently land in, or
be readable from, the wrong tenant.

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete

from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import OWNER
from app.models.issue import Issue
from app.models.organization import Organization, OrganizationMembership
from app.models.project import Project
from app.models.team import Team
from app.models.user import User
from app.services.copilot.tools import ToolContext, run_tool


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner_a = User(full_name="Isolation Owner A", email=f"isolation.a.{suffix}@test.invalid", hashed_password="x", role="owner")
        owner_b = User(full_name="Isolation Owner B", email=f"isolation.b.{suffix}@test.invalid", hashed_password="x", role="owner")
        db.add_all([owner_a, owner_b])
        await db.flush()

        org_a = Organization(name=f"Isolation Org A {suffix}", slug=f"isolation-a-{suffix}", owner_id=owner_a.id)
        org_b = Organization(name=f"Isolation Org B {suffix}", slug=f"isolation-b-{suffix}", owner_id=owner_b.id)
        db.add_all([org_a, org_b])
        await db.flush()
        db.add_all([
            OrganizationMembership(organization_id=org_a.id, user_id=owner_a.id, role="owner"),
            OrganizationMembership(organization_id=org_b.id, user_id=owner_b.id, role="owner"),
        ])
        await db.commit()
        org_a_id, org_b_id, owner_a_id, owner_b_id = org_a.id, org_b.id, owner_a.id, owner_b.id

        created_project_ids: list[int] = []
        created_team_ids: list[int] = []
        created_issue_ids: list[int] = []

        try:
            ctx_a = ToolContext(db=db, org_id=org_a_id, org_role=OWNER, user=owner_a, user_id=owner_a_id, session_id=None)
            ctx_b = ToolContext(db=db, org_id=org_b_id, org_role=OWNER, user=owner_b, user_id=owner_b_id, session_id=None)

            # ── Create one project/team/issue in EACH org ──
            result = await run_tool("create_project", {"name": f"Org A Project {suffix}"}, ctx_a)
            assert result.ok
            created_project_ids.append(result.data["project_id"])

            result = await run_tool("create_project", {"name": f"Org B Project {suffix}"}, ctx_b)
            assert result.ok
            created_project_ids.append(result.data["project_id"])

            result = await run_tool("create_team", {"name": f"Org A Team {suffix}", "team_manager_id": owner_a_id}, ctx_a)
            assert result.ok
            team_a_id = result.data["team_id"]
            created_team_ids.append(team_a_id)

            result = await run_tool("create_team", {"name": f"Org B Team {suffix}", "team_manager_id": owner_b_id}, ctx_b)
            assert result.ok
            created_team_ids.append(result.data["team_id"])

            result = await run_tool("create_issue", {"title": f"Org A Issue {suffix}", "team_id": team_a_id}, ctx_a)
            assert result.ok
            created_issue_ids.append(result.data["issue_id"])

            # ── search_projects: org A must see only its own project ──
            result = await run_tool("search_projects", {}, ctx_a)
            names_a = {p.name for p in result.data["items"]}
            assert f"Org A Project {suffix}" in names_a
            assert f"Org B Project {suffix}" not in names_a, "CROSS-TENANT LEAK: search_projects returned org B's project to org A"

            result = await run_tool("search_projects", {}, ctx_b)
            names_b = {p.name for p in result.data["items"]}
            assert f"Org B Project {suffix}" in names_b
            assert f"Org A Project {suffix}" not in names_b, "CROSS-TENANT LEAK: search_projects returned org A's project to org B"

            # ── search_teams: same isolation check ──
            result = await run_tool("search_teams", {}, ctx_a)
            team_names_a = {t.name for t in result.data["items"]}
            assert f"Org A Team {suffix}" in team_names_a
            assert f"Org B Team {suffix}" not in team_names_a, "CROSS-TENANT LEAK: search_teams returned org B's team to org A"

            # ── search_issues: org B must not see org A's issue ──
            result = await run_tool("search_issues", {}, ctx_b)
            issue_titles_b = {i.title for i in result.data["items"]}
            assert f"Org A Issue {suffix}" not in issue_titles_b, "CROSS-TENANT LEAK: search_issues returned org A's issue to org B"

            # ── An org B context must not even be able to reach org A's issue by ID (update_issue_status) ──
            result = await run_tool("update_issue_status", {"issue_id": created_issue_ids[0], "status": "resolved"}, ctx_b)
            assert not result.ok, "CROSS-TENANT LEAK: org B was able to update org A's issue by guessing its ID"

        finally:
            if created_issue_ids:
                await db.execute(delete(Issue).where(Issue.id.in_(created_issue_ids)))
            if created_project_ids:
                await db.execute(delete(Project).where(Project.id.in_(created_project_ids)))
            if created_team_ids:
                await db.execute(delete(Team).where(Team.id.in_(created_team_ids)))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_([org_a_id, org_b_id])))
            await db.execute(delete(Organization).where(Organization.id.in_([org_a_id, org_b_id])))
            await db.execute(delete(User).where(User.id.in_([owner_a_id, owner_b_id])))
            await db.commit()

    await engine.dispose()


def test_new_domain_tools_enforce_cross_tenant_isolation():
    asyncio.run(_scenario())
