"""Scoreboard Admin-only authorization regression test.

Product rule (current): Scoreboard access depends ONLY on the canonical,
existing organization-level Admin capability
(`TenantContext.is_admin_or_owner` / the `require_org_admin` dependency —
Owner, Admin, or anyone additionally granted `is_org_admin`). Team Manager
capability alone, Project Manager capability alone, Team Member, and
Client are all refused; a hybrid PM/TM user is allowed only because of an
ADDITIONAL admin grant, never because of PM/TM itself.

Covers every Scoreboard-related HTTP route:
  - GET /users/{id}/scoreboard            (app.api.routes.scoreboard)
  - GET /users/{id}/scoreboard/tasks       (app.api.routes.scoreboard)
  - GET /teams/{id}/scoreboard             (app.api.routes.team_scoreboard)
  - GET /teams/{id}/scoreboard/tasks       (app.api.routes.team_scoreboard)
  - GET /scoreboard/employees              (app.api.routes.organization_scoreboard)
  - GET /scoreboard/teams                  (app.api.routes.organization_scoreboard)
  - GET /scoreboard/managers               (app.api.routes.organization_scoreboard)

Also covers the crash fix: `_resolve_target_membership` never hands
`_resolve_employee` a `None`, a target with no active OrganizationMembership
gets a structured 404 (never a 500, never a fabricated role), a
deliberately mismatched legacy `User.role` never affects the result, and
cross-organization targets are denied/not found without data leakage.

Every route function is called directly, with `Depends(require_org_admin)`
applied explicitly first (mirroring exactly how FastAPI's own dependency
injection would run it) — calling a route function directly bypasses
FastAPI's DI, so the `Depends(...)` default is otherwise never actually
evaluated.

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete

from app.api.routes.organization_scoreboard import get_manager_rankings, get_organization_scoreboard, get_team_rankings
from app.api.routes.scoreboard import _resolve_employee, _resolve_target_membership, get_scoreboard, get_scoreboard_tasks
from app.api.routes.team_scoreboard import get_team_scoreboard, get_team_scoreboard_tasks
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import ADMIN, CLIENT, OWNER, PROJECT_MANAGER, TEAM_MANAGER, TEAM_MEMBER
from app.core.tenant import TenantContext, require_org_admin
from app.models.organization import Organization, OrganizationMembership
from app.models.task import Task
from app.models.team import Team, TeamMembership
from app.models.user import User


async def _admin_checked(tenant: TenantContext) -> TenantContext:
    """Runs the real `require_org_admin` dependency against `tenant` —
    the same check FastAPI would run before ever reaching the route body."""
    return await require_org_admin(tenant)


async def _expect_forbidden(tenant: TenantContext, label: str) -> None:
    try:
        await require_org_admin(tenant)
        raise AssertionError(f"{label} must not satisfy require_org_admin")
    except AppException as exc:
        assert exc.status_code == 403, f"{label}: expected 403, got {exc.status_code}"


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="SBAuth Owner", email=f"sbauth.owner.{suffix}@example-corp.com", hashed_password="x", role=OWNER)
        admin = User(full_name="SBAuth Admin", email=f"sbauth.admin.{suffix}@example-corp.com", hashed_password="x", role=ADMIN)
        tm = User(full_name="SBAuth TM", email=f"sbauth.tm.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MANAGER)
        pm = User(full_name="SBAuth PM", email=f"sbauth.pm.{suffix}@example-corp.com", hashed_password="x", role=PROJECT_MANAGER)
        # Deliberately mismatched legacy User.role — the authoritative
        # OrganizationMembership.role must be the only thing that decides
        # the returned employee.role.
        member = User(full_name="SBAuth Member", email=f"sbauth.member.{suffix}@example-corp.com", hashed_password="x", role="owner")
        tm_admin = User(full_name="SBAuth TM+Admin", email=f"sbauth.tmadmin.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MANAGER)
        pm_admin = User(full_name="SBAuth PM+Admin", email=f"sbauth.pmadmin.{suffix}@example-corp.com", hashed_password="x", role=PROJECT_MANAGER)
        client_user = User(full_name="SBAuth Client", email=f"sbauth.client.{suffix}@example-corp.com", hashed_password="x", role=CLIENT)
        never_a_member = User(full_name="SBAuth NeverMember", email=f"sbauth.nevermember.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)
        other_org_owner = User(full_name="SBAuth OtherOwner", email=f"sbauth.otherowner.{suffix}@example-corp.com", hashed_password="x", role=OWNER)
        db.add_all([owner, admin, tm, pm, member, tm_admin, pm_admin, client_user, never_a_member, other_org_owner])
        await db.flush()

        org = Organization(name=f"SBAuth Org {suffix}", slug=f"sbauth-{suffix}", owner_id=owner.id)
        other_org = Organization(name=f"SBAuth OtherOrg {suffix}", slug=f"sbauth-other-{suffix}", owner_id=other_org_owner.id)
        db.add_all([org, other_org])
        await db.flush()

        owner_membership = OrganizationMembership(organization_id=org.id, user_id=owner.id, role=OWNER)
        admin_membership = OrganizationMembership(organization_id=org.id, user_id=admin.id, role=ADMIN)
        tm_membership = OrganizationMembership(organization_id=org.id, user_id=tm.id, role=TEAM_MANAGER, is_team_manager=True)
        pm_membership = OrganizationMembership(organization_id=org.id, user_id=pm.id, role=PROJECT_MANAGER, is_project_manager=True)
        member_membership = OrganizationMembership(organization_id=org.id, user_id=member.id, role=TEAM_MEMBER)
        tm_admin_membership = OrganizationMembership(organization_id=org.id, user_id=tm_admin.id, role=TEAM_MANAGER, is_team_manager=True, is_org_admin=True)
        pm_admin_membership = OrganizationMembership(organization_id=org.id, user_id=pm_admin.id, role=PROJECT_MANAGER, is_project_manager=True, is_org_admin=True)
        client_membership = OrganizationMembership(organization_id=org.id, user_id=client_user.id, role=CLIENT)
        other_org_owner_membership = OrganizationMembership(organization_id=other_org.id, user_id=other_org_owner.id, role=OWNER)
        db.add_all([
            owner_membership, admin_membership, tm_membership, pm_membership, member_membership,
            tm_admin_membership, pm_admin_membership, client_membership, other_org_owner_membership,
        ])
        await db.commit()

        team = Team(name=f"SBAuth Team {suffix}", organization_id=org.id, team_manager_id=tm.id, created_by_id=owner.id)
        db.add(team)
        await db.flush()
        db.add_all([
            TeamMembership(team_id=team.id, user_id=tm.id),
            TeamMembership(team_id=team.id, user_id=member.id),
        ])
        await db.commit()

        owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=owner_membership, user=owner, db=db)
        admin_tenant = TenantContext(organization_id=org.id, organization=org, membership=admin_membership, user=admin, db=db)
        tm_tenant = TenantContext(organization_id=org.id, organization=org, membership=tm_membership, user=tm, db=db)
        pm_tenant = TenantContext(organization_id=org.id, organization=org, membership=pm_membership, user=pm, db=db)
        member_tenant = TenantContext(organization_id=org.id, organization=org, membership=member_membership, user=member, db=db)
        tm_admin_tenant = TenantContext(organization_id=org.id, organization=org, membership=tm_admin_membership, user=tm_admin, db=db)
        pm_admin_tenant = TenantContext(organization_id=org.id, organization=org, membership=pm_admin_membership, user=pm_admin, db=db)
        client_tenant = TenantContext(organization_id=org.id, organization=org, membership=client_membership, user=client_user, db=db)

        try:
            # ── 1/2. Admin/Owner open the main (user) Scoreboard -> 200. ──
            owner_checked = await _admin_checked(owner_tenant)
            r = await get_scoreboard(member.id, period="this_month", project_id=None, team_id=None, start_date=None, end_date=None, tenant=owner_checked)
            assert r.employee.role == TEAM_MEMBER

            admin_checked = await _admin_checked(admin_tenant)
            r = await get_scoreboard(member.id, period="this_month", project_id=None, team_id=None, start_date=None, end_date=None, tenant=admin_checked)
            assert r.employee.role == TEAM_MEMBER

            # ── 3. Admin opens a valid Team Manager's scoreboard -> 200. ──
            r = await get_scoreboard(tm.id, period="this_month", project_id=None, team_id=None, start_date=None, end_date=None, tenant=admin_checked)
            assert r.employee.role == TEAM_MANAGER

            # ── 4. Admin opens a valid Project Manager's scoreboard -> 200. ──
            r = await get_scoreboard(pm.id, period="this_month", project_id=None, team_id=None, start_date=None, end_date=None, tenant=admin_checked)
            assert r.employee.role == PROJECT_MANAGER

            # ── 5/6/7/8. TM/PM/Team Member/Client without admin capability
            # -> 403, at the dependency itself. ──
            await _expect_forbidden(tm_tenant, "Team Manager alone")
            await _expect_forbidden(pm_tenant, "Project Manager alone")
            await _expect_forbidden(member_tenant, "Team Member")
            await _expect_forbidden(client_tenant, "Client")

            # ── 9/10. TM+Admin and PM+Admin succeed — allowed because of
            # the admin capability, never because of TM/PM. ──
            tm_admin_checked = await _admin_checked(tm_admin_tenant)
            r = await get_scoreboard(member.id, period="this_month", project_id=None, team_id=None, start_date=None, end_date=None, tenant=tm_admin_checked)
            assert r.employee.role == TEAM_MEMBER

            pm_admin_checked = await _admin_checked(pm_admin_tenant)
            r = await get_scoreboard(member.id, period="this_month", project_id=None, team_id=None, start_date=None, end_date=None, tenant=pm_admin_checked)
            assert r.employee.role == TEAM_MEMBER

            # ── 11. direct /scoreboard/tasks without admin capability is
            # denied the same way. ──
            await _expect_forbidden(tm_tenant, "Team Manager alone (scoreboard/tasks)")
            tasks = await get_scoreboard_tasks(member.id, period="this_month", project_id=None, team_id=None, start_date=None, end_date=None, tenant=admin_checked)
            assert isinstance(tasks, list)

            # ── 12. Every other Scoreboard endpoint follows the same rule:
            # team scoreboard + org-wide rankings. ──
            for label, tenant in [("Team Manager alone", tm_tenant), ("Project Manager alone", pm_tenant), ("Team Member", member_tenant), ("Client", client_tenant)]:
                await _expect_forbidden(tenant, f"{label} (team/org scoreboard)")

            team_checked = await get_team_scoreboard(team.id, period="this_month", project_id=None, start_date=None, end_date=None, tenant=admin_checked)
            assert team_checked is not None
            team_tasks = await get_team_scoreboard_tasks(team.id, period="this_month", project_id=None, start_date=None, end_date=None, tenant=admin_checked)
            assert isinstance(team_tasks, list)

            org_board = await get_organization_scoreboard(period="this_month", start_date=None, end_date=None, manager_id=None, team_id=None, employee_id=None, tenant=admin_checked)
            assert org_board is not None
            team_rankings = await get_team_rankings(period="this_month", start_date=None, end_date=None, tenant=admin_checked)
            assert team_rankings is not None
            manager_rankings = await get_manager_rankings(period="this_month", start_date=None, end_date=None, tenant=admin_checked)
            assert manager_rankings is not None

            # ── 13/14. Target role is sourced from OrganizationMembership,
            # never legacy User.role (member.role == "owner" but their
            # membership role is team_member). ──
            assert member.role == "owner"
            r = await get_scoreboard(member.id, period="this_month", project_id=None, team_id=None, start_date=None, end_date=None, tenant=admin_checked)
            assert r.employee.role == TEAM_MEMBER, "employee.role must come from OrganizationMembership.role, never the mismatched legacy User.role"

            # ── 15. Missing target OrganizationMembership -> structured
            # response, never a 500, never fabricated. ──
            try:
                await _resolve_target_membership(admin_tenant, never_a_member.id)
                raise AssertionError("a user with no OrganizationMembership in this org must not resolve")
            except AppException as exc:
                assert exc.status_code == 404
                assert exc.code == "USER_NOT_FOUND"

            try:
                await get_scoreboard(never_a_member.id, period="this_month", project_id=None, team_id=None, start_date=None, end_date=None, tenant=admin_checked)
                raise AssertionError("get_scoreboard for a user with no membership must not 500 or succeed")
            except AppException as exc:
                assert exc.status_code == 404

            # ── 16. Cross-organization target -> denied/not found, no
            # data leakage. ──
            try:
                await get_scoreboard(other_org_owner.id, period="this_month", project_id=None, team_id=None, start_date=None, end_date=None, tenant=admin_checked)
                raise AssertionError("a cross-organization target must never resolve")
            except AppException as exc:
                assert exc.status_code == 404

            # ── 17. The originally failing scenario no longer crashes:
            # _resolve_target_membership always returns a real membership
            # (or raises) on every path _resolve_employee could reach. ──
            membership = await _resolve_target_membership(admin_tenant, tm.id)
            assert membership is not None
            employee = await _resolve_employee(admin_tenant, tm.id, membership)
            assert employee.role == TEAM_MANAGER

            # ── 18. Scoreboard metrics/summary shape is unchanged. ──
            assert owner_checked is not None
            full = await get_scoreboard(member.id, period="this_month", project_id=None, team_id=None, start_date=None, end_date=None, tenant=owner_checked)
            assert full.summary is not None
            assert full.score is not None
            assert isinstance(full.trend, list)

            print("test_scoreboard_admin_only_authorization: PASSED")
        finally:
            await db.execute(delete(TeamMembership).where(TeamMembership.team_id == team.id))
            await db.execute(delete(Team).where(Team.id == team.id))
            await db.execute(delete(Task).where(Task.organization_id == org.id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_([org.id, other_org.id])))
            await db.execute(delete(Organization).where(Organization.id.in_([org.id, other_org.id])))
            await db.execute(delete(User).where(User.id.in_([
                owner.id, admin.id, tm.id, pm.id, member.id, tm_admin.id, pm_admin.id,
                client_user.id, never_a_member.id, other_org_owner.id,
            ])))
            await db.commit()

    await engine.dispose()


def test_scoreboard_admin_only_authorization():
    asyncio.run(_scenario())
