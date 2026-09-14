"""Reporting + manager-tier Scoreboard regression test (architecture item
3 — "complete Reporting/Knowledge and Scoreboard capabilities required by
the architecture").

Covers:
- generate_project_report (AUTO/R2): creates a real Report row via the same
  ReportGenerationService the HTTP Reports module uses, RBAC refusal, and
  chat-wiring level. UNCHANGED by the Scoreboard authorization follow-up
  below — Reports' own viewing/generation rules are a separate feature.
- get_team_scoreboard / get_org_scoreboard (Scoreboard authorization
  follow-up, superseding rewrite): Scoreboard is now an ADMIN-ONLY chat
  tool surface too (app/services/copilot/tools/scoreboard_tools.py) — a
  Team Manager (even one who genuinely manages the team in question) or a
  Team Member (even one on the team's own roster) is refused; only Owner/
  Admin succeed. The previous "team_manager sees their own team,
  team_member sees their own roster" ABAC this file used to assert is no
  longer a valid product rule.

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete, select

from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import ADMIN, OWNER, TEAM_MANAGER, TEAM_MEMBER
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
        admin = User(full_name="Report Admin", email=f"report.admin.{suffix}@test.invalid", hashed_password="x", role="admin")
        manager_a = User(full_name="Report ManagerA", email=f"report.mgra.{suffix}@test.invalid", hashed_password="x", role="team_manager")
        manager_b = User(full_name="Report ManagerB", email=f"report.mgrb.{suffix}@test.invalid", hashed_password="x", role="team_manager")
        member = User(full_name="Report Member", email=f"report.member.{suffix}@test.invalid", hashed_password="x", role="team_member")
        db.add_all([owner, admin, manager_a, manager_b, member])
        await db.flush()

        org = Organization(name=f"Report Org {suffix}", slug=f"report-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.flush()
        db.add_all([
            OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner"),
            OrganizationMembership(organization_id=org.id, user_id=admin.id, role=ADMIN),
            OrganizationMembership(organization_id=org.id, user_id=manager_a.id, role="team_manager"),
            OrganizationMembership(organization_id=org.id, user_id=manager_b.id, role="team_manager"),
            OrganizationMembership(organization_id=org.id, user_id=member.id, role="team_member"),
        ])
        await db.commit()
        org_id, owner_id, admin_id, manager_a_id, manager_b_id, member_id = (
            org.id, owner.id, admin.id, manager_a.id, manager_b.id, member.id
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

            # ── get_team_scoreboard (Scoreboard authorization follow-up):
            # Admin-only now — manager_a is refused even for the team they
            # genuinely manage. ──
            mgr_a_ctx = ToolContext(db=db, org_id=org_id, org_role=TEAM_MANAGER, user=manager_a, user_id=manager_a_id, session_id=None)
            refused = await run_tool("get_team_scoreboard", {"team_id": team_a_id, "period": "this_month"}, mgr_a_ctx)
            assert not refused.ok, "a Team Manager alone (even for their own managed team) must be refused for get_team_scoreboard"

            mgr_b_ctx = ToolContext(db=db, org_id=org_id, org_role=TEAM_MANAGER, user=manager_b, user_id=manager_b_id, session_id=None)
            refused = await run_tool("get_team_scoreboard", {"team_id": team_a_id, "period": "this_month"}, mgr_b_ctx)
            assert not refused.ok, "a Team Manager who does not manage this team must be refused"

            # ── member (even on the team's own roster) is refused. ──
            refused = await run_tool("get_team_scoreboard", {"team_id": team_a_id, "period": "this_month"}, member_ctx)
            assert not refused.ok, "a Team Member (even on the team's own roster) must be refused for get_team_scoreboard"

            # ── Owner/Admin succeed. ──
            admin_ctx = ToolContext(db=db, org_id=org_id, org_role=ADMIN, user=admin, user_id=admin_id, session_id=None)
            result = await run_tool("get_team_scoreboard", {"team_id": team_a_id, "period": "this_month"}, owner_ctx)
            assert result.ok, f"Owner should be able to view any team's scoreboard, got: {result.message}"
            result = await run_tool("get_team_scoreboard", {"team_id": team_a_id, "period": "this_month"}, admin_ctx)
            assert result.ok, f"Admin should be able to view any team's scoreboard, got: {result.message}"

            # ── get_org_scoreboard: TEAM_MEMBER and Team Manager alone are
            # both refused entirely (no more "scoped to their own team"
            # partial access). ──
            refused = await run_tool("get_org_scoreboard", {"period": "this_month"}, member_ctx)
            assert not refused.ok, "a plain team_member must be refused for the org-wide scoreboard"
            refused = await run_tool("get_org_scoreboard", {"period": "this_month"}, mgr_a_ctx)
            assert not refused.ok, "a Team Manager alone must be refused for the org-wide scoreboard"

            # ── get_org_scoreboard: Owner/Admin succeed. ──
            result = await run_tool("get_org_scoreboard", {"period": "this_month"}, owner_ctx)
            assert result.ok, f"owner should be able to view the org-wide scoreboard, got: {result.message}"
            result = await run_tool("get_org_scoreboard", {"period": "this_month"}, admin_ctx)
            assert result.ok, f"admin should be able to view the org-wide scoreboard, got: {result.message}"

        finally:
            if report_ids:
                await db.execute(delete(Report).where(Report.id.in_(report_ids)))
            await db.execute(delete(TeamMembership).where(TeamMembership.team_id == team_a_id))
            await db.execute(delete(Team).where(Team.id == team_a_id))
            await db.execute(delete(Project).where(Project.id == project_id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org_id))
            await db.execute(delete(Organization).where(Organization.id == org_id))
            await db.execute(delete(User).where(User.id.in_([owner_id, admin_id, manager_a_id, manager_b_id, member_id])))
            await db.commit()

    await engine.dispose()


def test_reporting_and_manager_tier_scoreboards():
    asyncio.run(_scenario())
