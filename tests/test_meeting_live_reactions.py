"""Regression test for the live-meeting floating reaction system:
POST /meetings/{id}/reactions pushes an ephemeral (Redis-backed, never
persisted to Postgres) reaction, and GET /meetings/{id}/reactions?since=N
returns only what's new since the caller's last poll — the mechanism the
live meeting panel's existing ~2s sync poll piggybacks on to show other
participants' reactions without a page refresh.

Runs against the real database + Redis connections the app uses. Every row
this test creates is deleted before it returns; reaction entries expire on
their own (30s TTL) and are also explicitly cleared here.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete

from app.api.routes.meetings import create_meeting, list_meeting_reactions, send_meeting_reaction
from app.core.database import AsyncSessionLocal, engine
from app.core.redis_client import get_redis
from app.core.tenant import TenantContext
from app.models.meeting import Meeting
from app.models.organization import Organization, OrganizationMembership
from app.models.user import User
from app.schemas.meeting import MeetingCreate, MeetingReactionCreate


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="Reactions Owner", email=f"reactions.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        member = User(full_name="Reactions Member", email=f"reactions.member.{suffix}@test.invalid", hashed_password="x", role="team_member")
        db.add_all([owner, member])
        await db.flush()

        org = Organization(name=f"Reactions Org {suffix}", slug=f"reactions-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.flush()

        owner_membership = OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner")
        member_membership = OrganizationMembership(organization_id=org.id, user_id=member.id, role="team_member")
        db.add_all([owner_membership, member_membership])
        await db.commit()

        when = datetime.now(timezone.utc) + timedelta(days=1)
        owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=owner_membership, user=owner, db=db)
        member_tenant = TenantContext(organization_id=org.id, organization=org, membership=member_membership, user=member, db=db)

        # The member must be an explicitly-added attendee — meetings are
        # only accessible to whoever organizes them or is added as a
        # participant (see test_meeting_access_scoping_and_notifications.py),
        # so a plain team member with no participant row would correctly be
        # refused here, same as any other meeting sub-resource.
        meeting = await create_meeting(
            MeetingCreate(title=f"Reactions Test Meeting {suffix}", scheduled_at=when, meeting_type="level_10", participant_ids=[member.id]),
            db=db, tenant=owner_tenant,
        )

        try:
            # Nothing yet.
            initial = await list_meeting_reactions(meeting.id, since=0, db=db, tenant=owner_tenant)
            assert initial == []

            # Owner reacts. (send_meeting_reaction returns the raw dict from
            # push_reaction() when called directly like this, bypassing
            # FastAPI's response_model serialization into MeetingReactionOut.)
            r1 = await send_meeting_reaction(meeting.id, MeetingReactionCreate(emoji="👍"), db=db, tenant=owner_tenant)
            assert r1["emoji"] == "👍"
            assert r1["user_id"] == owner.id

            # A plain team member — an explicitly-added attendee on the same
            # meeting — reacts too. Ephemeral reactions aren't gated behind
            # any special role, matching notes/decisions.
            r2 = await send_meeting_reaction(meeting.id, MeetingReactionCreate(emoji="❤️"), db=db, tenant=member_tenant)
            assert r2["user_id"] == member.id
            assert r2["id"] > r1["id"], "reaction ids must be monotonically increasing per meeting"

            # A poller who's seen nothing yet gets both, in order.
            all_reactions = await list_meeting_reactions(meeting.id, since=0, db=db, tenant=owner_tenant)
            assert [r["emoji"] for r in all_reactions] == ["👍", "❤️"]

            # A poller who already rendered r1 only gets what's new (r2) —
            # this is what keeps a rapid burst of clicks from being
            # re-animated every single poll tick.
            only_new = await list_meeting_reactions(meeting.id, since=r1["id"], db=db, tenant=owner_tenant)
            assert len(only_new) == 1
            assert only_new[0]["id"] == r2["id"]

            # An invalid/unlisted emoji is rejected server-side (not just
            # trusted from the frontend's own button list).
            try:
                MeetingReactionCreate(emoji="💀")
                raise AssertionError("an emoji outside the allowed reaction set must be rejected")
            except Exception as exc:
                assert "emoji must be one of" in str(exc)

        finally:
            redis = await get_redis()
            await redis.delete(f"meeting:{meeting.id}:reactions", f"meeting:{meeting.id}:reactions:seq")
            await db.execute(delete(Meeting).where(Meeting.id == meeting.id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id == org.id))
            await db.execute(delete(User).where(User.id.in_([owner.id, member.id])))
            await db.commit()

    await engine.dispose()


def test_live_reactions_are_ephemeral_and_incremental_per_meeting():
    asyncio.run(_scenario())
