"""Regression test: a user whose base role is Project Manager, but who was
ALSO granted the "Team Manager" privilege flag and made a specific team's
manager.

Two DISTINCT things are covered here, deliberately kept apart:

1. `app.api.routes.team_scoreboard._require_can_view_team_scoreboard` — the
   ORIGINAL bug this test guarded (reported live, reproduced exactly): it
   branched on `tenant.org_role == "team_manager"` literally. A hybrid
   user — org_role literally "project_manager", is_team_manager=True, and
   team.team_manager_id == them — matched none of the role branches except
   the project_manager one (checks PROJECT membership via the team's
   tasks, an unrelated criterion) and incorrectly returned 403 for someone
   who legitimately manages the team outright. Fixed by delegating to the
   same flag-aware app.core.team_access rule already used for the team
   entity itself. This function is STILL used, unchanged, by
   app.api.routes.reports for its own Team Performance report-viewing
   rule — so this regression coverage still matters and is kept.

2. Scoreboard authorization follow-up (current product rule): the
   `GET /teams/{id}/scoreboard` ROUTE itself is now Admin-only
   (`Depends(require_org_admin)`) — a PM+TM hybrid WITHOUT admin
   capability must be refused for the route, for BOTH the team they
   manage and one they don't, even though `_require_can_view_team_scoreboard`
   above would have allowed the former. PM/TM capability, alone or
   combined, must never grant Scoreboard access by itself.

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete

from app.api.routes.team_scoreboard import _require_can_view_team_scoreboard, get_team_scoreboard
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import PROJECT_MANAGER
from app.core.tenant import TenantContext
from app.models.organization import Organization, OrganizationMembership
from app.models.team import Team
from app.models.user import User


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="HybridScore Owner", email=f"hybridscore.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        hybrid = User(full_name="HybridScore PM+TM", email=f"hybridscore.hybrid.{suffix}@test.invalid", hashed_password="x", role=PROJECT_MANAGER)
        db.add_all([owner, hybrid])
        await db.flush()

        org = Organization(name=f"HybridScore Org {suffix}", slug=f"hybridscore-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.flush()

        owner_membership = OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner")
        # The exact reported combination: role=project_manager AND the
        # is_team_manager privilege flag additionally granted.
        hybrid_membership = OrganizationMembership(organization_id=org.id, user_id=hybrid.id, role=PROJECT_MANAGER, is_team_manager=True)
        # Same hybrid identity, but ALSO granted admin capability — the
        # only combination the current product rule allows Scoreboard
        # access through.
        hybrid_admin_membership = OrganizationMembership(organization_id=org.id, user_id=hybrid.id, role=PROJECT_MANAGER, is_team_manager=True, is_org_admin=True)
        db.add_all([owner_membership, hybrid_membership])
        await db.commit()
        await db.refresh(hybrid_membership)

        team_a = Team(name=f"HybridScore Team A {suffix}", team_manager_id=hybrid.id, created_by_id=owner.id, organization_id=org.id)
        team_b = Team(name=f"HybridScore Team B {suffix}", team_manager_id=owner.id, created_by_id=owner.id, organization_id=org.id)
        db.add_all([team_a, team_b])
        await db.commit()

        hybrid_tenant = TenantContext(organization_id=org.id, organization=org, membership=hybrid_membership, user=hybrid, db=db)
        # Not persisted as a second row — same user, same org, just a
        # different in-memory TenantContext to exercise the is_org_admin
        # branch without a second OrganizationMembership row (which would
        # violate the one-membership-per-org-per-user invariant).
        hybrid_admin_tenant = TenantContext(organization_id=org.id, organization=org, membership=hybrid_admin_membership, user=hybrid, db=db)

        try:
            # ── 1. _require_can_view_team_scoreboard (still used by
            # Reports, unchanged): hybrid PM+TM succeeds for the team they
            # actually manage, still refused for one they don't. ──
            team_a_obj = await db.get(Team, team_a.id)
            await _require_can_view_team_scoreboard(hybrid_tenant, team_a_obj)  # must not raise

            team_b_obj = await db.get(Team, team_b.id)
            try:
                await _require_can_view_team_scoreboard(hybrid_tenant, team_b_obj)
                raise AssertionError("TEAM_SCOREBOARD_FORBIDDEN must be raised for a team this user doesn't manage")
            except AppException as exc:
                assert exc.code == "TEAM_SCOREBOARD_FORBIDDEN"
                assert exc.status_code == 403

            # ── 2. The actual GET /teams/{id}/scoreboard ROUTE is now
            # Admin-only — PM+TM capability alone, even for the team they
            # genuinely manage, is refused. Query(...)-defaulted params
            # must be passed explicitly since we're calling the route
            # function directly, bypassing FastAPI's dependency
            # resolution — but `Depends(require_org_admin)` still runs as
            # a real dependency wrapper, so it still enforces itself. ──
            from app.core.tenant import require_org_admin

            try:
                await require_org_admin(hybrid_tenant)
                raise AssertionError("PM+TM capability alone must never satisfy require_org_admin")
            except AppException as exc:
                assert exc.status_code == 403

            try:
                await get_team_scoreboard(
                    team_a.id, period="this_month", project_id=None, start_date=None, end_date=None,
                    tenant=await require_org_admin(hybrid_tenant),
                )
                raise AssertionError("PM+TM without admin capability must never reach the Team Scoreboard route, even for their own managed team")
            except AppException as exc:
                assert exc.status_code == 403

            # ── 3. PM+TM WITH admin capability succeeds — allowed because
            # of the admin capability, not because of PM/TM. ──
            admin_checked_tenant = await require_org_admin(hybrid_admin_tenant)
            result = await get_team_scoreboard(
                team_a.id, period="this_month", project_id=None, start_date=None, end_date=None,
                tenant=admin_checked_tenant,
            )
            assert result is not None
            # And now unrestricted to team_b too, purely via admin capability.
            result_b = await get_team_scoreboard(
                team_b.id, period="this_month", project_id=None, start_date=None, end_date=None,
                tenant=admin_checked_tenant,
            )
            assert result_b is not None

            print("test_team_scoreboard_hybrid_pm_tm_access: PASSED")
        finally:
            await db.execute(delete(Team).where(Team.id.in_([team_a.id, team_b.id])))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id == org.id))
            await db.execute(delete(User).where(User.id.in_([owner.id, hybrid.id])))
            await db.commit()

    await engine.dispose()


def test_hybrid_project_manager_with_team_manager_flag_can_view_own_team_scoreboard():
    asyncio.run(_scenario())
