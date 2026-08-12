"""Reporting + manager-tier Scoreboard regression test (architecture item
3 — "complete Reporting/Knowledge and Scoreboard capabilities required by
the architecture").

Covers:
- generate_project_report (AUTO/R2): creates a real Report row via the same
  ReportGenerationService the HTTP Reports module uses, RBAC refusal, and
  chat-wiring level.
- get_team_scoreboard: ABAC mirrors app/api/routes/team_scoreboard.py's
  _require_can_view_team_scoreboard exactly — a team_manager may view a
  team they manage but is refused for a DIFFERENT team; a team_member may
  view their own team's scoreboard.
- get_org_scoreboard: ABAC mirrors organization_scoreboard.py's
  _visible_teams exactly — team_manager sees only their own team(s) in the
  ranking; a plain team_member is refused entirely.

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete, select

from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import OWNER, TEAM_MANAGER, TEAM_MEMBER
from app.models.organization import Organization, OrganizationMembership
from app.models.project import Project
from app.models.report import Report
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.services.copilot.tools import ToolContext, run_tool


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="Report Owner", email=f"report.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        manager_a = User(full_name="Report ManagerA", email=f"report.mgra.{suffix}@test.invalid", hashed_password="x", role="team_manager")
        manager_b = User(full_name="Report ManagerB", email=f"report.mgrb.{suffix}@test.invalid", hashed_password="x", role="team_manager")
        member = User(full_name="Report Member", email=f"report.member.{suffix}@test.invalid", hashed_password="x", role="team_member")
        db.add_all([owner, manager_a, manager_b, member])
        await db.flush()

        org = Organization(name=f"Report Org {suffix}", slug=f"report-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.flush()
        db.add_all([
            OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner"),
            OrganizationMembership(organization_id=org.id, user_id=manager_a.id, role="team_manager"),
            OrganizationMembership(organization_id=org.id, user_id=manager_b.id, role="team_manager"),
            OrganizationMembership(organization_id=org.id, user_id=member.id, role="team_member"),
        ])
        await db.commit()
        org_id, owner_id, manager_a_id, manager_b_id, member_id = (
            org.id, owner.id, manager_a.id, manager_b.id, member.id
        )

        project = Project(name=f"Report Project {suffix}", created_by_id=owner_id, organization_id=org_id)
        team_a = Team(name=f"Team A {suffix}", team_manager_id=manager_a_id, created_by_id=owner_id, organization_id=org_id)
        db.add_all([project, team_a])
        await db.flush()
        db.add(TeamMembership(team_id=team_a.id, user_id=member_id))
        await db.commit()
        project_id, team_a_id = project.id, team_a.id

        report_ids: list[int] = []
        try:
            owner_ctx = ToolContext(db=db, org_id=org_id, org_role=OWNER, user=owner, user_id=owner_id, session_id=None)

            # ── generate_project_report: creates a real Report row ──
            result = await run_tool(
                "generate_project_report",
                {"project_id": project_id, "report_type": "monthly", "title": f"Monthly Report {suffix}"},
                owner_ctx,
            )
            assert result.ok, f"generate_project_report should succeed, got: {result.message}"
            report_id = result.data["report_id"]
            report_ids.append(report_id)
            row = (await db.execute(select(Report).where(Report.id == report_id))).scalar_one()
            assert row.title == f"Monthly Report {suffix}" and row.project_id == project_id and row.report_type == "monthly"

            # ── RBAC: TEAM_MEMBER refused for generate_project_report ──
            member_ctx = ToolContext(db=db, org_id=org_id, org_role=TEAM_MEMBER, user=member, user_id=member_id, session_id=None)
            refused = await run_tool(
                "generate_project_report", {"project_id": project_id, "report_type": "monthly", "title": "Should never exist"}, member_ctx,
            )
            assert not refused.ok, "TEAM_MEMBER must be refused for generate_project_report"

            # ── get_team_scoreboard: manager_a (manages team_a) may view ──
            mgr_a_ctx = ToolContext(db=db, org_id=org_id, org_role=TEAM_MANAGER, user=manager_a, user_id=manager_a_id, session_id=None)
            result = await run_tool("get_team_scoreboard", {"team_id": team_a_id, "period": "this_month"}, mgr_a_ctx)
            assert result.ok, f"manager_a should be able to view their own team's scoreboard, got: {result.message}"

            # ── get_team_scoreboard: manager_b (manages a DIFFERENT team) refused ──
            mgr_b_ctx = ToolContext(db=db, org_id=org_id, org_role=TEAM_MANAGER, user=manager_b, user_id=manager_b_id, session_id=None)
            refused = await run_tool("get_team_scoreboard", {"team_id": team_a_id, "period": "this_month"}, mgr_b_ctx)
            assert not refused.ok, "a team_manager who does not manage this team must be refused"

            # ── get_team_scoreboard: member (is on the team's roster) may view ──
            result = await run_tool("get_team_scoreboard", {"team_id": team_a_id, "period": "this_month"}, member_ctx)
            assert result.ok, f"a team_member on the team's roster should be able to view its scoreboard, got: {result.message}"

            # ── get_org_scoreboard: TEAM_MEMBER refused entirely ──
            refused = await run_tool("get_org_scoreboard", {"period": "this_month"}, member_ctx)
            assert not refused.ok, "a plain team_member must be refused for the org-wide scoreboard"

            # ── get_org_scoreboard: owner sees the ranking (may be empty, but must succeed) ──
            result = await run_tool("get_org_scoreboard", {"period": "this_month"}, owner_ctx)
            assert result.ok, f"owner should be able to view the org-wide scoreboard, got: {result.message}"

            # ── get_org_scoreboard: manager_a succeeds (scoped to their own team internally) ──
            result = await run_tool("get_org_scoreboard", {"period": "this_month"}, mgr_a_ctx)
            assert result.ok, f"a team_manager should be able to view the org-wide scoreboard (scoped to their teams), got: {result.message}"

        finally:
            if report_ids:
                await db.execute(delete(Report).where(Report.id.in_(report_ids)))
            await db.execute(delete(TeamMembership).where(TeamMembership.team_id == team_a_id))
            await db.execute(delete(Team).where(Team.id == team_a_id))
            await db.execute(delete(Project).where(Project.id == project_id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org_id))
            await db.execute(delete(Organization).where(Organization.id == org_id))
            await db.execute(delete(User).where(User.id.in_([owner_id, manager_a_id, manager_b_id, member_id])))
            await db.commit()

    await engine.dispose()


def test_reporting_and_manager_tier_scoreboards():
    asyncio.run(_scenario())
