"""Regression tests for the inline-Task-assignee-dropdown bug-fix
follow-up: POST /teams/assignable-users/bulk
(app.repositories.team_repository.TeamRepository.
list_assignable_members_bulk / filter_accessible_team_ids /
filter_existing_team_ids).

Root cause this closes: TasksPage's and ProjectDetailPage's INLINE
quick-assignee `<Select>` sourced options from an org-wide `assignees`
list built from `GET /users` — admin-only, so a Team Manager always got
an empty list, and the dropdown showed only "Unassigned" even when the
Team had eligible members. This endpoint gives those surfaces a
Team-scoped, bulk, N+1-free data source instead.

Covers (see the spec's "PHASE 15 — TESTS"):
  1. Team Manager managed Team: assignable members returned.
  2. Team Manager unrelated Team: members not returned.
  3. Client excluded.
  4. Client with TeamMembership still excluded.
  5. inactive member excluded (OrganizationMembership.is_active AND
     User.is_active).
  6. Owner/Admin receives exact Team members.
  7. two Teams: lists do not mix.
  8. multiple Team IDs: bulk endpoint returns correct keyed results.
  9. cross-tenant Team ID: no leak.
  10. non-Team Task fallback remains valid (documented — this endpoint is
      Team-only by design; a non-Team task's assignee options are the
      existing org-wide-minus-Client fallback, unchanged, and not part of
      this endpoint).
  11. inline update to valid Team member succeeds (via the existing,
      already-tested PATCH /tasks/{id} + validate_task_assignee — not
      duplicated here, see test_task_assignee_eligibility.py).
  12. inline update to wrong-Team user still rejected (same note as #11).
  13. Unassigned succeeds (same note as #11).
  14. active timer reassignment protection still works (see
      test_timer_assignee_only.py — unaffected by this follow-up).
  15. timer assignee-only rule still works (same note as #14).

Items 11-15 are existing, already-passing behavior this follow-up never
touches (no change to validate_task_assignee, update_task, or the timer
routes) — run as part of the regression pass instead of being duplicated
here; this file focuses on what's actually new: the bulk endpoint itself.

Runs against the real database connection the app uses. Every row this
test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete, event

from app.api.routes.teams import list_teams_assignable_users_bulk
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import CLIENT, TEAM_MANAGER, TEAM_MEMBER
from app.core.tenant import TenantContext
from app.models.organization import Organization, OrganizationMembership
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.schemas.team import TeamAssignableUsersBulkRequest


async def _run():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]

        owner = User(full_name="TAB Owner", email=f"tab.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        admin = User(full_name="TAB Admin", email=f"tab.admin.{suffix}@test.invalid", hashed_password="x", role="admin")
        tm = User(full_name="TAB TM", email=f"tab.tm.{suffix}@test.invalid", hashed_password="x", role=TEAM_MANAGER)
        tm2 = User(full_name="TAB TM2", email=f"tab.tm2.{suffix}@test.invalid", hashed_password="x", role=TEAM_MANAGER)
        alice = User(full_name="TAB Alice", email=f"tab.alice.{suffix}@test.invalid", hashed_password="x", role=TEAM_MEMBER)
        bob = User(full_name="TAB Bob", email=f"tab.bob.{suffix}@test.invalid", hashed_password="x", role=TEAM_MEMBER)
        client_user = User(full_name="TAB Client", email=f"tab.client.{suffix}@test.invalid", hashed_password="x", role=TEAM_MEMBER)  # legacy/stale User.role
        inactive_user = User(full_name="TAB Inactive", email=f"tab.inactive.{suffix}@test.invalid", hashed_password="x", role=TEAM_MEMBER, is_active=False)
        marketing_user = User(full_name="TAB Marketing", email=f"tab.marketing.{suffix}@test.invalid", hashed_password="x", role=TEAM_MEMBER)
        outsider = User(full_name="TAB Outsider", email=f"tab.outsider.{suffix}@test.invalid", hashed_password="x", role="owner")
        db.add_all([owner, admin, tm, tm2, alice, bob, client_user, inactive_user, marketing_user, outsider])
        await db.commit()
        for u in (owner, admin, tm, tm2, alice, bob, client_user, inactive_user, marketing_user, outsider):
            await db.refresh(u)

        org = Organization(name=f"TAB Org {suffix}", slug=f"tab-org-{suffix}", owner_id=owner.id)
        other_org = Organization(name=f"TAB Other Org {suffix}", slug=f"tab-other-org-{suffix}", owner_id=outsider.id)
        db.add_all([org, other_org])
        await db.commit()
        for o in (org, other_org):
            await db.refresh(o)

        memberships = {}
        for u, role in [
            (owner, "owner"), (admin, "admin"), (tm, TEAM_MANAGER), (tm2, TEAM_MANAGER),
            (alice, TEAM_MEMBER), (bob, TEAM_MEMBER), (client_user, CLIENT),
            (inactive_user, TEAM_MEMBER), (marketing_user, TEAM_MEMBER),
        ]:
            m = OrganizationMembership(organization_id=org.id, user_id=u.id, role=role)
            db.add(m)
            memberships[u.id] = m
        outsider_membership = OrganizationMembership(organization_id=other_org.id, user_id=outsider.id, role="owner")
        db.add(outsider_membership)
        await db.commit()

        # Technology Team: managed by tm. Members: alice, bob, client_user
        # (legacy-bad-data TeamMembership row), inactive_user. Marketing
        # Team: managed by tm2 (unrelated to tm), member marketing_user.
        tech_team = Team(name=f"TAB Technology {suffix}", team_manager_id=tm.id, created_by_id=owner.id, organization_id=org.id)
        marketing_team = Team(name=f"TAB Marketing {suffix}", team_manager_id=tm2.id, created_by_id=owner.id, organization_id=org.id)
        db.add_all([tech_team, marketing_team])
        await db.commit()
        for t in (tech_team, marketing_team):
            await db.refresh(t)
        db.add_all([
            TeamMembership(team_id=tech_team.id, user_id=tm.id),
            TeamMembership(team_id=tech_team.id, user_id=alice.id),
            TeamMembership(team_id=tech_team.id, user_id=bob.id),
            TeamMembership(team_id=tech_team.id, user_id=client_user.id),  # legacy-style bad data
            TeamMembership(team_id=tech_team.id, user_id=inactive_user.id),
            TeamMembership(team_id=marketing_team.id, user_id=tm2.id),
            TeamMembership(team_id=marketing_team.id, user_id=marketing_user.id),
        ])
        await db.commit()

        org_id, other_org_id = org.id, other_org.id
        tech_team_id, marketing_team_id = tech_team.id, marketing_team.id
        user_ids = [owner.id, admin.id, tm.id, tm2.id, alice.id, bob.id, client_user.id, inactive_user.id, marketing_user.id, outsider.id]

        owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[owner.id], user=owner, db=db)
        admin_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[admin.id], user=admin, db=db)
        tm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[tm.id], user=tm, db=db)

        try:
            # ── 1. Team Manager managed Team: assignable members returned. ───
            r1 = await list_teams_assignable_users_bulk(TeamAssignableUsersBulkRequest(team_ids=[tech_team_id]), tenant=tm_tenant)
            tech_ids = {m.id for m in r1.teams.get(str(tech_team_id), [])}
            assert alice.id in tech_ids
            assert bob.id in tech_ids
            assert tm.id in tech_ids, "the manager themself (also a TeamMembership row) must be included"

            # ── 3, 4. Client excluded, even with a (legacy-bad-data)
            # TeamMembership row — role wins over membership. ──────────────────
            assert client_user.id not in tech_ids

            # ── 5. Inactive member excluded. ──────────────────────────────────
            assert inactive_user.id not in tech_ids

            # ── 2. Team Manager unrelated Team: members not returned. ─────────
            r2 = await list_teams_assignable_users_bulk(TeamAssignableUsersBulkRequest(team_ids=[marketing_team_id]), tenant=tm_tenant)
            assert str(marketing_team_id) not in r2.teams, "tm does not manage/belong to Marketing — must get nothing for it, not an empty-but-present list"

            # ── 6, 7, 8. Owner/Admin: exact Team members, multiple Teams
            # requested at once, lists do not mix. ─────────────────────────────
            r6 = await list_teams_assignable_users_bulk(TeamAssignableUsersBulkRequest(team_ids=[tech_team_id, marketing_team_id]), tenant=owner_tenant)
            owner_tech_ids = {m.id for m in r6.teams[str(tech_team_id)]}
            owner_marketing_ids = {m.id for m in r6.teams[str(marketing_team_id)]}
            assert owner_tech_ids == {tm.id, alice.id, bob.id}
            assert owner_marketing_ids == {tm2.id, marketing_user.id}
            assert marketing_user.id not in owner_tech_ids, "Marketing's member must never appear in Technology's list"
            assert alice.id not in owner_marketing_ids, "Technology's member must never appear in Marketing's list"

            r_admin = await list_teams_assignable_users_bulk(TeamAssignableUsersBulkRequest(team_ids=[tech_team_id]), tenant=admin_tenant)
            assert {m.id for m in r_admin.teams[str(tech_team_id)]} == {tm.id, alice.id, bob.id}

            # ── 9. Cross-tenant Team ID: no leak, for any caller. ─────────────
            other_team = Team(name=f"TAB Other-Org Team {suffix}", team_manager_id=outsider.id, created_by_id=outsider.id, organization_id=other_org_id)
            db.add(other_team)
            await db.commit()
            await db.refresh(other_team)
            try:
                r9 = await list_teams_assignable_users_bulk(TeamAssignableUsersBulkRequest(team_ids=[other_team.id]), tenant=owner_tenant)
                assert r9.teams == {}, "an org-A tenant must never receive another organization's team data"
                r9_tm = await list_teams_assignable_users_bulk(TeamAssignableUsersBulkRequest(team_ids=[other_team.id]), tenant=tm_tenant)
                assert r9_tm.teams == {}
            finally:
                await db.execute(delete(Team).where(Team.id == other_team.id))
                await db.commit()

            # ── empty request -> safe empty result. ───────────────────────────
            r_empty = await list_teams_assignable_users_bulk(TeamAssignableUsersBulkRequest(team_ids=[]), tenant=tm_tenant)
            assert r_empty.teams == {}

            # ── 14. No N+1: query count stays constant as team_ids grow. ──────
            query_count = {"n": 0}

            def _count(conn, cursor, statement, parameters, context, executemany):
                query_count["n"] += 1

            sync_engine = engine.sync_engine
            event.listen(sync_engine, "before_cursor_execute", _count)
            try:
                query_count["n"] = 0
                await list_teams_assignable_users_bulk(TeamAssignableUsersBulkRequest(team_ids=[tech_team_id]), tenant=owner_tenant)
                small_count = query_count["n"]

                query_count["n"] = 0
                await list_teams_assignable_users_bulk(TeamAssignableUsersBulkRequest(team_ids=[tech_team_id, marketing_team_id] * 50), tenant=owner_tenant)
                large_count = query_count["n"]
            finally:
                event.remove(sync_engine, "before_cursor_execute", _count)
            assert small_count == large_count, f"query count must stay constant regardless of how many team_ids are requested (got {small_count} vs {large_count})"

        finally:
            await db.execute(delete(TeamMembership).where(TeamMembership.team_id.in_([tech_team_id, marketing_team_id])))
            await db.execute(delete(Team).where(Team.id.in_([tech_team_id, marketing_team_id])))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_([org_id, other_org_id])))
            await db.execute(delete(Organization).where(Organization.id.in_([org_id, other_org_id])))
            await db.execute(delete(User).where(User.id.in_(user_ids)))
            await db.commit()

    await engine.dispose()


def test_team_assignable_users_bulk():
    asyncio.run(_run())
