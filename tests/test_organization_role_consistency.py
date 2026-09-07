"""Regression tests for the Organization Role Consistency bug fix.

Root cause (see final report): `GET /users/{id}/scoreboard` populated its
`employee.role` field straight from the legacy, non-org-specific
`User.role` column instead of the authoritative `OrganizationMembership.role`
for the active organization — so a user shown as "Owner" on the Users list
(already correctly sourced from membership) could show as "Team Member" on
their own Scorecard header. The same stale-source bug also existed in
`GET /teams/{id}` (and `GET /teams`)'s `members`/`team_manager` role field,
and in the Team/Employee Performance PDF reports.

Covers (see PHASE 21 of the spec):
  1. membership.role=owner + stale User.role=team_member -> scoreboard
     reports owner.
  2. Users list and User Detail (scoreboard) agree for the same user.
  3. admin membership -> reports admin.
  4. team_manager membership -> reports team_manager.
  5. team_member membership -> reports team_member.
  6. client membership is never fabricated as team_member (scoreboard is
     N/A for clients regardless of the stale legacy value).
  7. multi-org user: org A membership=owner / org B membership=team_member
     resolve independently per active organization.
  8. cross-tenant membership never leaks (a user with no membership in the
     asking org is treated as not found, not resolved from another org).
  9. missing/inactive membership never silently fabricates "team_member".
  10. a role update (PATCH /users/{id}) is reflected on the very next
      scoreboard read — no stale caching.
  11. scorecard metrics (summary/score numbers) are unaffected by this fix.
  12. GET /teams/{id} member/team_manager role also reflects
      OrganizationMembership.role, not the legacy column — closing the
      same class of bug in a second, previously-undiscovered location.

Runs against the real database connection the app uses. Every row this
test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete, select

from app.api.routes.scoreboard import _resolve_employee, _require_can_view_scoreboard, get_scoreboard
from app.api.routes.teams import get_team
from app.api.routes.users import list_users, update_user
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import ADMIN, CLIENT, OWNER, TEAM_MANAGER, TEAM_MEMBER
from app.core.tenant import TenantContext
from app.models.organization import Organization, OrganizationMembership
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.schemas.user import UserUpdate


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]

        # Deliberately mismatched: User.role (legacy) says one thing,
        # OrganizationMembership.role (authoritative) says another — this
        # is exactly the "Muzahid Al Moon" bug scenario from the report.
        owner = User(full_name="RC Owner", email=f"rc.owner.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)
        admin = User(full_name="RC Admin", email=f"rc.admin.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)
        tm = User(full_name="RC TM", email=f"rc.tm.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)
        member = User(full_name="RC Member", email=f"rc.member.{suffix}@example-corp.com", hashed_password="x", role=OWNER)
        client_user = User(full_name="RC Client", email=f"rc.client.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)
        multi = User(full_name="RC Multi", email=f"rc.multi.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)
        outsider = User(full_name="RC Outsider", email=f"rc.outsider.{suffix}@example-corp.com", hashed_password="x", role="owner")
        db.add_all([owner, admin, tm, member, client_user, multi, outsider])
        await db.commit()
        for u in (owner, admin, tm, member, client_user, multi, outsider):
            await db.refresh(u)

        org = Organization(name=f"RC Org {suffix}", slug=f"rc-org-{suffix}", owner_id=owner.id)
        org_b = Organization(name=f"RC Org B {suffix}", slug=f"rc-org-b-{suffix}", owner_id=outsider.id)
        db.add_all([org, org_b])
        await db.commit()
        await db.refresh(org)
        await db.refresh(org_b)

        memberships = {}
        for u, role, extra in [
            (owner, OWNER, {}),
            (admin, ADMIN, {"is_org_admin": True}),
            (tm, TEAM_MANAGER, {"is_team_manager": True}),
            (member, TEAM_MEMBER, {}),
            (client_user, CLIENT, {}),
            (multi, OWNER, {}),  # multi's role in org (org A)
        ]:
            m = OrganizationMembership(organization_id=org.id, user_id=u.id, role=role, **extra)
            db.add(m)
            memberships[u.id] = m
        outsider_membership = OrganizationMembership(organization_id=org_b.id, user_id=outsider.id, role=OWNER)
        # multi ALSO belongs to org B, with a DIFFERENT role (team_member).
        multi_membership_b = OrganizationMembership(organization_id=org_b.id, user_id=multi.id, role=TEAM_MEMBER)
        db.add_all([outsider_membership, multi_membership_b])
        await db.commit()
        for m in list(memberships.values()) + [outsider_membership, multi_membership_b]:
            await db.refresh(m)

        owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[owner.id], user=owner, db=db)
        admin_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[admin.id], user=admin, db=db)
        outsider_tenant = TenantContext(organization_id=org_b.id, organization=org_b, membership=outsider_membership, user=outsider, db=db)
        multi_tenant_b = TenantContext(organization_id=org_b.id, organization=org_b, membership=multi_membership_b, user=multi, db=db)

        created_team_ids: list[int] = []

        try:
            # ── 1. membership=owner, stale User.role=team_member -> Owner. ──
            membership = await _require_can_view_scoreboard(admin_tenant, owner.id)
            employee = await _resolve_employee(admin_tenant, owner.id, membership)
            assert employee.role == OWNER, "scorecard must report the org membership role, never the stale legacy User.role"
            assert owner.role == TEAM_MEMBER, "sanity check: the legacy column really is stale/mismatched"

            # ── 2. Users list and User Detail agree. ─────────────────────────
            users_list = await list_users(tenant=admin_tenant, db=db)
            owner_row = next(u for u in users_list if u.id == owner.id)
            assert owner_row.role == OWNER
            assert owner_row.role == employee.role, "Users list and Scorecard header must never disagree"

            # ── 3, 4, 5. admin / team_manager / team_member. ──────────────────
            admin_membership = await _require_can_view_scoreboard(admin_tenant, admin.id)
            admin_employee = await _resolve_employee(admin_tenant, admin.id, admin_membership)
            assert admin_employee.role == ADMIN

            tm_membership = await _require_can_view_scoreboard(admin_tenant, tm.id)
            tm_employee = await _resolve_employee(admin_tenant, tm.id, tm_membership)
            assert tm_employee.role == TEAM_MANAGER

            member_membership = await _require_can_view_scoreboard(admin_tenant, member.id)
            member_employee = await _resolve_employee(admin_tenant, member.id, member_membership)
            assert member_employee.role == TEAM_MEMBER
            assert member.role == OWNER, "sanity check: legacy column mismatched the other direction too"

            # ── 6. Client never resolves (scoreboard N/A) and is never
            # fabricated as team_member. ──────────────────────────────────────
            try:
                await _require_can_view_scoreboard(admin_tenant, client_user.id)
                raise AssertionError("a Client must never resolve a scoreboard")
            except AppException as exc:
                assert exc.code == "SCOREBOARD_NOT_APPLICABLE"

            # ── 7. Multi-org user resolves independently per active org. ──────
            multi_membership_a = await _require_can_view_scoreboard(admin_tenant, multi.id)
            multi_employee_a = await _resolve_employee(admin_tenant, multi.id, multi_membership_a)
            assert multi_employee_a.role == OWNER, "org A membership for this user is owner"

            multi_membership_b_resolved = await _require_can_view_scoreboard(multi_tenant_b, multi.id)
            multi_employee_b = await _resolve_employee(multi_tenant_b, multi.id, multi_membership_b_resolved)
            assert multi_employee_b.role == TEAM_MEMBER, "org B membership for the SAME user is team_member — must not bleed from org A"

            # ── 8. Cross-tenant membership never leaks: org B's admin
            # (outsider) asking about `owner` (a member only of org A) must
            # get "not found", never org A's role resolved through org B. ────
            try:
                await _require_can_view_scoreboard(outsider_tenant, owner.id)
                raise AssertionError("a user with no membership in the asking org must not resolve")
            except AppException as exc:
                assert exc.code == "USER_NOT_FOUND"

            # ── 9. Missing/inactive membership never fabricates team_member. ──
            never_a_member = User(full_name="RC Nobody", email=f"rc.nobody.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)
            db.add(never_a_member)
            await db.commit()
            await db.refresh(never_a_member)
            try:
                await _require_can_view_scoreboard(admin_tenant, never_a_member.id)
                raise AssertionError("a user with no membership at all in this org must not resolve")
            except AppException as exc:
                assert exc.code == "USER_NOT_FOUND"
            await db.execute(delete(User).where(User.id == never_a_member.id))
            await db.commit()

            # ── 10. Role update is reflected on the very next read. ────────────
            await update_user(member.id, UserUpdate(role=TEAM_MANAGER), tenant=admin_tenant, db=db)
            refreshed_membership = await _require_can_view_scoreboard(admin_tenant, member.id)
            refreshed_employee = await _resolve_employee(admin_tenant, member.id, refreshed_membership)
            assert refreshed_employee.role == TEAM_MANAGER, "a role change must be visible on the very next scoreboard read, no stale caching"
            # restore
            await update_user(member.id, UserUpdate(role=TEAM_MEMBER), tenant=admin_tenant, db=db)

            # ── 11. Scorecard metrics are unaffected by this fix — the
            # summary/score shape is untouched, only the role source changed. ──
            response = await get_scoreboard(
                owner.id, period="this_month", project_id=None, team_id=None,
                start_date=None, end_date=None, tenant=admin_tenant,
            )
            assert response.employee.role == OWNER
            assert response.summary.total_assigned == 0
            assert response.summary.total_completed == 0
            assert response.summary.completion_rate == 0
            assert response.score.rounded_score is not None or response.score.has_data is False

            # ── 12. GET /teams/{id}'s member/team_manager role also reflects
            # org membership, not the legacy column — same bug, second
            # location. ─────────────────────────────────────────────────────
            team = Team(name=f"RC Team {suffix}", team_manager_id=tm.id, created_by_id=owner.id, organization_id=org.id)
            db.add(team)
            await db.flush()
            db.add(TeamMembership(team_id=team.id, user_id=member.id))
            await db.commit()
            await db.refresh(team)
            created_team_ids.append(team.id)

            team_detail = await get_team(team.id, tenant=owner_tenant)
            assert team_detail.team_manager.role == TEAM_MANAGER, "team_manager's org role must come from OrganizationMembership, not stale User.role"
            member_row = next(m for m in team_detail.members if m.id == member.id)
            assert member_row.role == TEAM_MEMBER, "team member's org role must come from OrganizationMembership, not stale User.role"
            # (member.role's legacy value was already exercised as a mismatch
            # earlier in this scenario — step 10 above intentionally changed
            # it via update_user(), so it's no longer a useful mismatch probe
            # by this point.)

        finally:
            if created_team_ids:
                await db.execute(delete(TeamMembership).where(TeamMembership.team_id.in_(created_team_ids)))
                await db.execute(delete(Team).where(Team.id.in_(created_team_ids)))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_([org.id, org_b.id])))
            await db.execute(delete(Organization).where(Organization.id.in_([org.id, org_b.id])))
            all_user_ids = [owner.id, admin.id, tm.id, member.id, client_user.id, multi.id, outsider.id]
            await db.execute(delete(User).where(User.id.in_(all_user_ids)))
            await db.commit()

    await engine.dispose()


def test_organization_role_consistency():
    asyncio.run(_scenario())
