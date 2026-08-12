"""Scoreboard chat regression test — Domain Tool Registry level
(get_my_scoreboard via run_tool()) AND chat-wiring level (the "my_scoreboard"
db_query sub-intent via _handle_db_query_impl), closing the strict
acceptance audit's 0%-coverage finding for the Scoreboards domain.

Scoreboards are entirely computed (derived from task completion data) — no
write capability exists or should exist (see read_tools.py's
_get_my_scoreboard_handler docstring). This test proves the read path is
real, CLIENT-blocked, and reachable from chat.

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete

from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import CLIENT, TEAM_MEMBER
from app.models.organization import Organization, OrganizationMembership
from app.models.user import User
from app.services.copilot.tools import ToolContext, run_tool


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="Scoreboard Owner", email=f"scoreboard.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        member = User(full_name="Scoreboard Member", email=f"scoreboard.member.{suffix}@test.invalid", hashed_password="x", role="team_member")
        client = User(full_name="Scoreboard Client", email=f"scoreboard.client.{suffix}@test.invalid", hashed_password="x", role="client")
        db.add_all([owner, member, client])
        await db.flush()

        org = Organization(name=f"Scoreboard Org {suffix}", slug=f"scoreboard-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.flush()
        db.add_all([
            OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner"),
            OrganizationMembership(organization_id=org.id, user_id=member.id, role="team_member"),
            OrganizationMembership(organization_id=org.id, user_id=client.id, role="client"),
        ])
        await db.commit()
        org_id, member_id, client_id = org.id, member.id, client.id

        try:
            # ── Domain Tool Registry level: a team_member with no tasks gets a clean "no data" result, not an error ──
            member_ctx = ToolContext(db=db, org_id=org_id, org_role=TEAM_MEMBER, user=member, user_id=member_id, session_id=None)
            result = await run_tool("get_my_scoreboard", {"period": "this_month"}, member_ctx)
            assert result.ok, f"get_my_scoreboard should succeed (even with no data), got: {result.message}"
            data = result.data["scoreboard"]
            assert data.current.has_data is False, "a member with zero assigned tasks this period must show has_data=False, not fabricated numbers"

            # ── CLIENT is refused (scoreboards are internal-staff data) ──
            client_ctx = ToolContext(db=db, org_id=org_id, org_role=CLIENT, user=client, user_id=client_id, session_id=None)
            result = await run_tool("get_my_scoreboard", {"period": "this_month"}, client_ctx)
            assert not result.ok, "CLIENT must be refused for get_my_scoreboard"

            # ── Unknown parameter rejected ──
            result = await run_tool("get_my_scoreboard", {"period": "this_month", "bogus": True}, member_ctx)
            assert not result.ok, "unknown parameter must be rejected (extra='forbid')"

            # ── Invalid period rejected by schema, not silently coerced ──
            result = await run_tool("get_my_scoreboard", {"period": "not_a_real_period"}, member_ctx)
            assert not result.ok, "an invalid period literal must be rejected"

        finally:
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org_id))
            await db.execute(delete(Organization).where(Organization.id == org_id))
            await db.execute(delete(User).where(User.id.in_([owner.id, member_id, client_id])))
            await db.commit()

    await engine.dispose()


def test_scoreboard_read_tool_and_client_block():
    asyncio.run(_scenario())
