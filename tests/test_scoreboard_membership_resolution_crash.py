"""Scoreboard 500 crash regression test.

Root cause: `_require_can_view_scoreboard`'s Team-Manager access branch
used a bare `return` — returning `None` on every successful Team Manager
access path, instead of the already-resolved `target_membership`.
`get_scoreboard()` then passed that `None` straight into `_resolve_employee`,
which immediately dereferenced `membership.role` -> AttributeError -> 500.
`GET /scoreboard/tasks` never crashed because it calls
`_require_can_view_scoreboard` for its side effect only and never touches
the returned value's `.role`.

Scoreboard authorization follow-up (superseding rewrite): Scoreboard is now
an ADMIN-ONLY feature — a Team Manager (or Project Manager) no longer has
ANY Scoreboard access, crash or no crash, so the original "TM viewer
succeeds" scenario this file guarded is no longer a valid product rule and
has been replaced below. What remains permanently true, and is what this
file now guards: `get_scoreboard()`'s target-membership resolution
(`_resolve_target_membership`) must NEVER be able to hand `_resolve_employee`
a `None` on any successful (Admin) access path — the crash's actual root
cause — and a viewer without the canonical Admin capability, TM/PM
included, must get a clean 403, never a 500, never data.

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete

from app.api.routes.scoreboard import _resolve_employee, _resolve_target_membership, get_scoreboard, get_scoreboard_tasks
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import OWNER, TEAM_MANAGER, TEAM_MEMBER
from app.core.tenant import TenantContext, require_org_admin
from app.models.organization import Organization, OrganizationMembership
from app.models.team import Team, TeamMembership
from app.models.user import User


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="SBCrash Owner", email=f"sbcrash.owner.{suffix}@example-corp.com", hashed_password="x", role="owner")
        admin = User(full_name="SBCrash Admin", email=f"sbcrash.admin.{suffix}@example-corp.com", hashed_password="x", role="admin")
        tm = User(full_name="SBCrash TM", email=f"sbcrash.tm.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MANAGER)
        member = User(full_name="SBCrash Member", email=f"sbcrash.member.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)
        db.add_all([owner, admin, tm, member])
        await db.flush()

        org = Organization(name=f"SBCrash Org {suffix}", slug=f"sbcrash-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.flush()

        owner_membership = OrganizationMembership(organization_id=org.id, user_id=owner.id, role=OWNER)
        admin_membership = OrganizationMembership(organization_id=org.id, user_id=admin.id, role="admin", is_org_admin=True)
        tm_membership = OrganizationMembership(organization_id=org.id, user_id=tm.id, role=TEAM_MANAGER, is_team_manager=True)
        member_membership = OrganizationMembership(organization_id=org.id, user_id=member.id, role=TEAM_MEMBER)
        db.add_all([owner_membership, admin_membership, tm_membership, member_membership])
        await db.commit()

        team = Team(name=f"SBCrash Team {suffix}", organization_id=org.id, team_manager_id=tm.id, created_by_id=owner.id)
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

        try:
            # ── The original crash, now via the only viewer who can
            # legitimately reach _resolve_employee at all (Admin): must
            # not 500, and must never hand a None membership through. ──
            membership = await _resolve_target_membership(admin_tenant, member.id)
            assert membership is not None, "a successful target-membership resolution must never be None"

            employee = await _resolve_employee(admin_tenant, member.id, membership)
            assert employee.role == TEAM_MEMBER, "role must come from OrganizationMembership.role"

            # ── Full endpoint function, end-to-end, for an actual Admin
            # (FastAPI's Depends(require_org_admin) applied explicitly,
            # matching how the real dependency injection would run it). ──
            admin_checked = await require_org_admin(admin_tenant)
            response = await get_scoreboard(
                member.id, period="this_month", project_id=None, team_id=None,
                start_date=None, end_date=None, tenant=admin_checked,
            )
            assert response.employee.role == TEAM_MEMBER
            assert response.employee.id == member.id

            owner_checked = await require_org_admin(owner_tenant)
            owner_response = await get_scoreboard(
                member.id, period="this_month", project_id=None, team_id=None,
                start_date=None, end_date=None, tenant=owner_checked,
            )
            assert owner_response.employee.role == TEAM_MEMBER

            # ── /scoreboard/tasks remains working for Admin. ──
            tasks = await get_scoreboard_tasks(
                member.id, period="this_month", project_id=None, team_id=None,
                start_date=None, end_date=None, tenant=admin_checked,
            )
            assert isinstance(tasks, list)

            # ── A Team Manager viewing a team member's scoreboard — the
            # exact scenario that used to 500 — now gets a clean 403 at
            # the authorization dependency itself, never a crash, never
            # data (Scoreboard is Admin-only; TM capability alone grants
            # nothing). ──
            try:
                await require_org_admin(tm_tenant)
                raise AssertionError("Team Manager capability alone must never satisfy require_org_admin")
            except AppException as exc:
                assert exc.status_code == 403

            print("test_scoreboard_membership_resolution_crash: PASSED")
        finally:
            await db.execute(delete(TeamMembership).where(TeamMembership.team_id == team.id))
            await db.execute(delete(Team).where(Team.id == team.id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id == org.id))
            await db.execute(delete(User).where(User.id.in_([owner.id, admin.id, tm.id, member.id])))
            await db.commit()

    await engine.dispose()


def test_scoreboard_membership_resolution_crash():
    asyncio.run(_scenario())
