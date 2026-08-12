"""Regression test: a user whose base role is Project Manager, but who was
ALSO granted the "Team Manager" privilege flag and made a specific team's
manager, must be able to view that team's Scoreboard.

BUG BEING GUARDED AGAINST (reported live, reproduced exactly): GET
/teams/{id}/scoreboard's _require_can_view_team_scoreboard() branched on
`tenant.org_role == "team_manager"` literally. A hybrid user — org_role
literally "project_manager", is_team_manager=True, and
team.team_manager_id == them — matched none of the role branches except
the project_manager one, which checks PROJECT membership via the team's
tasks (an unrelated criterion), and incorrectly returned 403
("You do not have permission to view this team's scoreboard.") for someone
who legitimately manages the team outright.

Fixed by delegating to the same flag-aware app.core.team_access rule
already used for the team entity itself (and Rocks/Issues/KPIs/Team News).

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete

from app.api.routes.team_scoreboard import get_team_scoreboard
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
        db.add_all([owner_membership, hybrid_membership])
        await db.commit()
        await db.refresh(hybrid_membership)

        team_a = Team(name=f"HybridScore Team A {suffix}", team_manager_id=hybrid.id, created_by_id=owner.id, organization_id=org.id)
        team_b = Team(name=f"HybridScore Team B {suffix}", team_manager_id=owner.id, created_by_id=owner.id, organization_id=org.id)
        db.add_all([team_a, team_b])
        await db.commit()

        hybrid_tenant = TenantContext(organization_id=org.id, organization=org, membership=hybrid_membership, user=hybrid, db=db)

        try:
            # Must succeed for the team they actually manage.
            # Query(...)-defaulted params must be passed explicitly here since
            # we're calling the route function directly, bypassing FastAPI's
            # dependency injection that would normally resolve them.
            result = await get_team_scoreboard(
                team_a.id, period="this_month", project_id=None,
                start_date=None, end_date=None, tenant=hybrid_tenant,
            )
            assert result is not None, "hybrid PM+TM must be able to view the scoreboard of the team they manage"

            # Must still be refused for a team they don't manage.
            try:
                await get_team_scoreboard(
                    team_b.id, period="this_month", project_id=None,
                    start_date=None, end_date=None, tenant=hybrid_tenant,
                )
                raise AssertionError("TEAM_SCOREBOARD_FORBIDDEN must be raised for a team this user doesn't manage")
            except AppException as exc:
                assert exc.code == "TEAM_SCOREBOARD_FORBIDDEN"
                assert exc.status_code == 403

        finally:
            await db.execute(delete(Team).where(Team.id.in_([team_a.id, team_b.id])))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id == org.id))
            await db.execute(delete(User).where(User.id.in_([owner.id, hybrid.id])))
            await db.commit()

    await engine.dispose()


def test_hybrid_project_manager_with_team_manager_flag_can_view_own_team_scoreboard():
    asyncio.run(_scenario())
