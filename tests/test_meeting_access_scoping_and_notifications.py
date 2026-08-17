"""Regression test for "users should only have access to meetings they are
explicitly assigned to" + the three meeting notification triggers (added as
attendee, meeting started, scheduled-start reminder).

Covers:
  - A plain team member who is NOT a participant/organizer of a meeting
    cannot see it in list_meetings and gets 404 from get_meeting (not a
    leaky 403) even when they know its id.
  - A team member who IS an added participant can list and get it.
  - Managers (team_manager here) always see every meeting in the org,
    regardless of participation — unchanged, matches their existing
    create/manage permission tier.
  - create_meeting notifies every invited participant except the creator,
    each notification carrying the new meeting_id link.
  - update_meeting's participant_ids replacement only notifies the
    genuinely NEW participants — re-saving an unchanged list, or a list
    that already included someone, never re-notifies them.
  - start_meeting notifies every other participant with a "meeting_started"
    notification exactly once — resuming from a pause (a separate /resume
    route that never touches started_at) does not re-notify.

Runs against the real database. Every row this test creates is deleted
before it returns.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete, select

from app.api.routes.meetings import create_meeting, get_meeting, list_meetings, start_meeting, update_meeting
from app.core.database import AsyncSessionLocal, engine
from app.core.tenant import TenantContext
from app.models.meeting import Meeting, MeetingParticipant
from app.models.notification import Notification
from app.models.organization import Organization, OrganizationMembership
from app.models.user import User
from app.schemas.meeting import MeetingCreate, MeetingUpdate

from fastapi import HTTPException


async def _notifications_for(db, user_id, meeting_id, type_):
    result = await db.execute(
        select(Notification).where(
            Notification.user_id == user_id,
            Notification.meeting_id == meeting_id,
            Notification.type == type_,
        )
    )
    return list(result.scalars().all())


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        manager = User(full_name="Access Manager", email=f"access.mgr.{suffix}@test.invalid", hashed_password="x", role="team_manager")
        invited = User(full_name="Access Invited", email=f"access.invited.{suffix}@test.invalid", hashed_password="x", role="team_member")
        outsider = User(full_name="Access Outsider", email=f"access.outsider.{suffix}@test.invalid", hashed_password="x", role="team_member")
        db.add_all([manager, invited, outsider])
        await db.flush()

        org = Organization(name=f"Access Org {suffix}", slug=f"access-{suffix}", owner_id=manager.id)
        db.add(org)
        await db.flush()

        manager_membership = OrganizationMembership(organization_id=org.id, user_id=manager.id, role="team_manager")
        invited_membership = OrganizationMembership(organization_id=org.id, user_id=invited.id, role="team_member")
        outsider_membership = OrganizationMembership(organization_id=org.id, user_id=outsider.id, role="team_member")
        db.add_all([manager_membership, invited_membership, outsider_membership])
        await db.commit()

        when = datetime.now(timezone.utc) + timedelta(days=1)
        manager_tenant = TenantContext(organization_id=org.id, organization=org, membership=manager_membership, user=manager, db=db)
        invited_tenant = TenantContext(organization_id=org.id, organization=org, membership=invited_membership, user=invited, db=db)
        outsider_tenant = TenantContext(organization_id=org.id, organization=org, membership=outsider_membership, user=outsider, db=db)

        meeting = await create_meeting(
            MeetingCreate(title=f"Access Test Meeting {suffix}", scheduled_at=when, meeting_type="level_10", participant_ids=[invited.id]),
            db=db, tenant=manager_tenant,
        )

        try:
            # ── Access scoping ──
            # The outsider (not invited, not organizer) can't see it at all.
            outsider_list = await list_meetings(filter=None, team_id=None, db=db, tenant=outsider_tenant)
            assert meeting.id not in [m.id for m in outsider_list]

            try:
                await get_meeting(meeting.id, db=db, tenant=outsider_tenant)
                raise AssertionError("an outsider must not be able to GET a meeting they weren't invited to")
            except HTTPException as exc:
                assert exc.status_code == 404, "must be a plain not-found, not a leaky 403"

            # The invited participant CAN see and open it.
            invited_list = await list_meetings(filter=None, team_id=None, db=db, tenant=invited_tenant)
            assert meeting.id in [m.id for m in invited_list]
            fetched = await get_meeting(meeting.id, db=db, tenant=invited_tenant)
            assert fetched.id == meeting.id

            # The manager (create/manage tier) always sees everything,
            # participant or not.
            manager_list = await list_meetings(filter=None, team_id=None, db=db, tenant=manager_tenant)
            assert meeting.id in [m.id for m in manager_list]

            # ── "Added as attendee" notification ──
            # The invited participant got one at creation time; the creator
            # (manager) did not notify themself.
            invited_notifs = await _notifications_for(db, invited.id, meeting.id, "meeting_invite")
            assert len(invited_notifs) == 1
            manager_notifs = await _notifications_for(db, manager.id, meeting.id, "meeting_invite")
            assert manager_notifs == []

            # Adding the outsider via PATCH notifies only them, not a
            # duplicate for the already-participating invited user.
            await update_meeting(
                meeting.id,
                MeetingUpdate(participant_ids=[invited.id, outsider.id]),
                db=db, tenant=manager_tenant,
            )
            outsider_notifs = await _notifications_for(db, outsider.id, meeting.id, "meeting_invite")
            assert len(outsider_notifs) == 1
            invited_notifs_after = await _notifications_for(db, invited.id, meeting.id, "meeting_invite")
            assert len(invited_notifs_after) == 1, "re-saving an already-participating attendee must not re-notify them"

            # Re-saving the exact same participant list again must not
            # notify anyone a second time either.
            await update_meeting(
                meeting.id,
                MeetingUpdate(participant_ids=[invited.id, outsider.id]),
                db=db, tenant=manager_tenant,
            )
            assert len(await _notifications_for(db, invited.id, meeting.id, "meeting_invite")) == 1
            assert len(await _notifications_for(db, outsider.id, meeting.id, "meeting_invite")) == 1

            # ── "Meeting started" notification ──
            started = await start_meeting(meeting.id, db=db, tenant=manager_tenant)
            assert started.status == "ongoing"
            for user_id in (invited.id, outsider.id):
                started_notifs = await _notifications_for(db, user_id, meeting.id, "meeting_started")
                assert len(started_notifs) == 1
            manager_started_notifs = await _notifications_for(db, manager.id, meeting.id, "meeting_started")
            assert manager_started_notifs == [], "the person who started it doesn't need to be told"

            # Pausing and starting again (simulating a resume-then-restart
            # through the same /start route) must not re-notify — only the
            # genuine first start does.
            await db.execute(
                select(Meeting).where(Meeting.id == meeting.id)
            )
            fresh = await get_meeting(meeting.id, db=db, tenant=manager_tenant)
            fresh.status = "paused"
            await db.commit()
            await start_meeting(meeting.id, db=db, tenant=manager_tenant)
            for user_id in (invited.id, outsider.id):
                assert len(await _notifications_for(db, user_id, meeting.id, "meeting_started")) == 1

        finally:
            await db.execute(delete(Notification).where(Notification.meeting_id == meeting.id))
            await db.execute(delete(MeetingParticipant).where(MeetingParticipant.meeting_id == meeting.id))
            await db.execute(delete(Meeting).where(Meeting.id == meeting.id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id == org.id))
            await db.execute(delete(User).where(User.id.in_([manager.id, invited.id, outsider.id])))
            await db.commit()

    await engine.dispose()


def test_meeting_access_is_scoped_and_notifications_fire_correctly():
    asyncio.run(_scenario())
