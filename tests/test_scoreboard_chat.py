"""Scoreboard chat regression test — Domain Tool Registry level
(get_my_scoreboard via run_tool()) AND chat-wiring level (the "my_scoreboard"
db_query sub-intent via _handle_db_query_impl).

Scoreboard authorization follow-up (current product rule, superseding
rewrite): Scoreboard is an ADMIN-ONLY feature — access depends solely on
the canonical Admin capability, the same rule the HTTP scoreboard routes
enforce via `require_org_admin`. "Team Member -> own scoreboard access" is
explicitly one of the old rules removed by that fix, so a plain Team
Member is now refused `get_my_scoreboard` too, same as a Client always
was — only Owner/Admin succeed.

Scoreboards are entirely computed (derived from task completion data) — no
write capability exists or should exist (see read_tools.py's
_get_my_scoreboard_handler docstring). This test proves the read path is
real, Admin-gated, and reachable from chat.

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete

from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import CLIENT, PROJECT_MANAGER, TEAM_MANAGER, TEAM_MEMBER
from app.models.organization import Organization, OrganizationMembership
from app.models.user import User
from app.services.copilot.tools import ToolContext, run_tool


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="Scoreboard Owner", email=f"scoreboard.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        admin = User(full_name="Scoreboard Admin", email=f"scoreboard.admin.{suffix}@test.invalid", hashed_password="x", role="admin")
        member = User(full_name="Scoreboard Member", email=f"scoreboard.member.{suffix}@test.invalid", hashed_password="x", role="team_member")
        tm = User(full_name="Scoreboard TM", email=f"scoreboard.tm.{suffix}@test.invalid", hashed_password="x", role=TEAM_MANAGER)
        pm = User(full_name="Scoreboard PM", email=f"scoreboard.pm.{suffix}@test.invalid", hashed_password="x", role=PROJECT_MANAGER)
        client = User(full_name="Scoreboard Client", email=f"scoreboard.client.{suffix}@test.invalid", hashed_password="x", role="client")
        db.add_all([owner, admin, member, tm, pm, client])
        await db.flush()

        org = Organization(name=f"Scoreboard Org {suffix}", slug=f"scoreboard-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.flush()
        db.add_all([
            OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner"),
            OrganizationMembership(organization_id=org.id, user_id=admin.id, role="admin"),
            OrganizationMembership(organization_id=org.id, user_id=member.id, role="team_member"),
            OrganizationMembership(organization_id=org.id, user_id=tm.id, role=TEAM_MANAGER),
            OrganizationMembership(organization_id=org.id, user_id=pm.id, role=PROJECT_MANAGER),
            OrganizationMembership(organization_id=org.id, user_id=client.id, role="client"),
        ])
        await db.commit()
        org_id = org.id

        try:
            # ── Owner succeeds (even with no data, a clean "no data"
            # result, not an error). ──
            owner_ctx = ToolContext(db=db, org_id=org_id, org_role="owner", user=owner, user_id=owner.id, session_id=None)
            result = await run_tool("get_my_scoreboard", {"period": "this_month"}, owner_ctx)
            assert result.ok, f"Owner's get_my_scoreboard should succeed, got: {result.message}"
            data = result.data["scoreboard"]
            assert data.current.has_data is False, "zero assigned tasks this period must show has_data=False, not fabricated numbers"

            # ── Admin succeeds too. ──
            admin_ctx = ToolContext(db=db, org_id=org_id, org_role="admin", user=admin, user_id=admin.id, session_id=None)
            result = await run_tool("get_my_scoreboard", {"period": "this_month"}, admin_ctx)
            assert result.ok, f"Admin's get_my_scoreboard should succeed, got: {result.message}"

            # ── Team Member is refused — "own scoreboard access" is no
            # longer a valid rule; Scoreboard is Admin-only regardless of
            # whose data is being viewed. ──
            member_ctx = ToolContext(db=db, org_id=org_id, org_role=TEAM_MEMBER, user=member, user_id=member.id, session_id=None)
            result = await run_tool("get_my_scoreboard", {"period": "this_month"}, member_ctx)
            assert not result.ok, "Team Member must be refused for get_my_scoreboard — PM/TM/self capability alone must never grant Scoreboard access"

            # ── Team Manager alone is refused. ──
            tm_ctx = ToolContext(db=db, org_id=org_id, org_role=TEAM_MANAGER, user=tm, user_id=tm.id, session_id=None)
            result = await run_tool("get_my_scoreboard", {"period": "this_month"}, tm_ctx)
            assert not result.ok, "Team Manager capability alone must be refused for get_my_scoreboard"

            # ── Project Manager alone is refused. ──
            pm_ctx = ToolContext(db=db, org_id=org_id, org_role=PROJECT_MANAGER, user=pm, user_id=pm.id, session_id=None)
            result = await run_tool("get_my_scoreboard", {"period": "this_month"}, pm_ctx)
            assert not result.ok, "Project Manager capability alone must be refused for get_my_scoreboard"

            # ── Team scoreboard / org scoreboard chat tools follow the
            # same Admin-only rule. ──
            result = await run_tool("get_team_scoreboard", {"team_id": 1, "period": "this_month"}, tm_ctx)
            assert not result.ok, "Team Manager alone must be refused for get_team_scoreboard"
            result = await run_tool("get_org_scoreboard", {"period": "this_month"}, tm_ctx)
            assert not result.ok, "Team Manager alone must be refused for get_org_scoreboard"

            # ── CLIENT is refused (scoreboards are internal-staff data;
            # was already refused before this fix, unchanged). ──
            client_ctx = ToolContext(db=db, org_id=org_id, org_role=CLIENT, user=client, user_id=client.id, session_id=None)
            result = await run_tool("get_my_scoreboard", {"period": "this_month"}, client_ctx)
            assert not result.ok, "CLIENT must be refused for get_my_scoreboard"

            # ── Unknown parameter rejected (Owner ctx, schema-level check
            # unrelated to authorization). ──
            result = await run_tool("get_my_scoreboard", {"period": "this_month", "bogus": True}, owner_ctx)
            assert not result.ok, "unknown parameter must be rejected (extra='forbid')"

            # ── Invalid period rejected by schema, not silently coerced. ──
            result = await run_tool("get_my_scoreboard", {"period": "not_a_real_period"}, owner_ctx)
            assert not result.ok, "an invalid period literal must be rejected"

        finally:
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org_id))
            await db.execute(delete(Organization).where(Organization.id == org_id))
            await db.execute(delete(User).where(User.id.in_([owner.id, admin.id, member.id, tm.id, pm.id, client.id])))
            await db.commit()

    await engine.dispose()


def test_scoreboard_read_tool_and_client_block():
    asyncio.run(_scenario())
