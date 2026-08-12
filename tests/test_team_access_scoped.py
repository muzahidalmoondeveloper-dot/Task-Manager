"""Team access scope regression test — the team-side counterpart to
app.core.project_access's project rule, per the same live-confirmed
decision: nobody except Owner/Admin sees every team by default.

- A Project Manager (base role or granted flag) no longer sees/accesses
  every team read-only "for selection purposes" — only team(s) they
  actually manage (team_manager_id) or have been given membership on.
- A Team Manager only sees/accesses team(s) they actually manage.
- A Team Member only sees/accesses team(s) they're a member of.
- Owner/Admin: unrestricted, as before.

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete

from app.api.routes.teams import get_team, list_teams
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import PROJECT_MANAGER, TEAM_MANAGER, TEAM_MEMBER
from app.core.tenant import TenantContext
from app.models.organization import Organization, OrganizationMembership
from app.models.team import Team, TeamMembership
from app.models.user import User


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="TeamScope Owner", email=f"teamscope.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        pm = User(full_name="TeamScope PM", email=f"teamscope.pm.{suffix}@test.invalid", hashed_password="x", role=PROJECT_MANAGER)
        tm = User(full_name="TeamScope TM", email=f"teamscope.tm.{suffix}@test.invalid", hashed_password="x", role=TEAM_MANAGER)
        member = User(full_name="TeamScope Member", email=f"teamscope.member.{suffix}@test.invalid", hashed_password="x", role=TEAM_MEMBER)
        db.add_all([owner, pm, tm, member])
        await db.flush()

        org = Organization(name=f"TeamScope Org {suffix}", slug=f"teamscope-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.flush()

        owner_membership = OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner")
        pm_membership = OrganizationMembership(organization_id=org.id, user_id=pm.id, role=PROJECT_MANAGER)
        tm_membership = OrganizationMembership(organization_id=org.id, user_id=tm.id, role=TEAM_MANAGER)
        member_membership = OrganizationMembership(organization_id=org.id, user_id=member.id, role=TEAM_MEMBER)
        db.add_all([owner_membership, pm_membership, tm_membership, member_membership])
        await db.commit()
        for m in (owner_membership, pm_membership, tm_membership, member_membership):
            await db.refresh(m)

        team_a = Team(name=f"Team A {suffix}", team_manager_id=tm.id, created_by_id=owner.id, organization_id=org.id)
        team_b = Team(name=f"Team B {suffix}", team_manager_id=owner.id, created_by_id=owner.id, organization_id=org.id)
        db.add_all([team_a, team_b])
        await db.flush()
        # `member` is on team A's roster; `pm` is given access to team B via membership (rule 4).
        db.add(TeamMembership(team_id=team_a.id, user_id=member.id))
        db.add(TeamMembership(team_id=team_b.id, user_id=pm.id))
        await db.commit()

        pm_tenant = TenantContext(organization_id=org.id, organization=org, membership=pm_membership, user=pm, db=db)
        tm_tenant = TenantContext(organization_id=org.id, organization=org, membership=tm_membership, user=tm, db=db)
        member_tenant = TenantContext(organization_id=org.id, organization=org, membership=member_membership, user=member, db=db)
        owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=owner_membership, user=owner, db=db)

        try:
            # ── Team Manager sees only the team they manage (A), not B. ──
            visible_to_tm = await list_teams(tenant=tm_tenant)
            assert {t.name for t in visible_to_tm} == {team_a.name}
            fetched = await get_team(team_a.id, tenant=tm_tenant)
            assert fetched.id == team_a.id
            try:
                await get_team(team_b.id, tenant=tm_tenant)
                raise AssertionError("TEAM_NOT_ASSIGNED must be raised for a team this Team Manager doesn't manage")
            except AppException as exc:
                assert exc.code == "TEAM_NOT_ASSIGNED"
                assert exc.status_code == 403

            # ── Project Manager no longer sees every team read-only — only
            #    team B, which they were given membership on. ──
            visible_to_pm = await list_teams(tenant=pm_tenant)
            assert {t.name for t in visible_to_pm} == {team_b.name}, (
                "a Project Manager must no longer see every team in the org — only ones they're assigned to"
            )
            fetched = await get_team(team_b.id, tenant=pm_tenant)
            assert fetched.id == team_b.id
            try:
                await get_team(team_a.id, tenant=pm_tenant)
                raise AssertionError("TEAM_NOT_ASSIGNED must be raised for a team the PM has no access to")
            except AppException as exc:
                assert exc.code == "TEAM_NOT_ASSIGNED"

            # ── Team Member sees only the team(s) they're a member of. ──
            visible_to_member = await list_teams(tenant=member_tenant)
            assert {t.name for t in visible_to_member} == {team_a.name}
            try:
                await get_team(team_b.id, tenant=member_tenant)
                raise AssertionError("TEAM_NOT_ASSIGNED must be raised for a team this member isn't on")
            except AppException as exc:
                assert exc.code == "TEAM_NOT_ASSIGNED"

            # ── Owner: unrestricted. ──
            visible_to_owner = await list_teams(tenant=owner_tenant)
            assert {t.name for t in visible_to_owner} == {team_a.name, team_b.name}

        finally:
            await db.execute(delete(TeamMembership).where(TeamMembership.team_id.in_([team_a.id, team_b.id])))
            await db.execute(delete(Team).where(Team.id.in_([team_a.id, team_b.id])))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id == org.id))
            await db.execute(delete(User).where(User.id.in_([owner.id, pm.id, tm.id, member.id])))
            await db.commit()

    await engine.dispose()


def test_team_access_is_scoped_to_managed_or_member_teams_only():
    asyncio.run(_scenario())
