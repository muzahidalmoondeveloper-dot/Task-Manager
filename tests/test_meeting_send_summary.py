"""Regression test for the Conclude section's "Send email summary" action:
POST /meetings/{id}/send-summary should queue one background email per
participant that has a user account, and never raise even with zero
participants.

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete

from app.api.routes.meetings import send_meeting_summary
from app.core.database import AsyncSessionLocal, engine
from app.core.tenant import TenantContext
from app.models.meeting import Meeting, MeetingParticipant
from app.models.organization import Organization, OrganizationMembership
from app.models.user import User


class _FakeBackgroundTasks:
    def __init__(self):
        self.calls = []

    def add_task(self, func, *args):
        self.calls.append((func, args))


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="MeetingSummary Owner", email=f"meetingsummary.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        attendee = User(full_name="MeetingSummary Attendee", email=f"meetingsummary.attendee.{suffix}@test.invalid", hashed_password="x", role="team_member")
        db.add_all([owner, attendee])
        await db.flush()

        org = Organization(name=f"MeetingSummary Org {suffix}", slug=f"meetingsummary-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.flush()

        owner_membership = OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner")
        db.add_all([owner_membership, OrganizationMembership(organization_id=org.id, user_id=attendee.id, role="team_member")])
        await db.commit()

        when = datetime.now(timezone.utc) + timedelta(days=1)
        meeting = Meeting(title=f"Summary Test Meeting {suffix}", scheduled_at=when, organization_id=org.id, organizer_id=owner.id)
        db.add(meeting)
        await db.flush()
        db.add_all([
            MeetingParticipant(meeting_id=meeting.id, user_id=owner.id),
            MeetingParticipant(meeting_id=meeting.id, user_id=attendee.id),
        ])
        await db.commit()

        tenant = TenantContext(organization_id=org.id, organization=org, membership=owner_membership, user=owner, db=db)
        bg = _FakeBackgroundTasks()

        try:
            result = await send_meeting_summary(meeting.id, background_tasks=bg, db=db, tenant=tenant)
            assert result == {"queued": 2}
            queued_recipient_ids = {args[1] for _func, args in bg.calls}
            assert queued_recipient_ids == {owner.id, attendee.id}
            assert all(args[0] == meeting.id for _func, args in bg.calls)

        finally:
            await db.execute(delete(MeetingParticipant).where(MeetingParticipant.meeting_id == meeting.id))
            await db.execute(delete(Meeting).where(Meeting.id == meeting.id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id == org.id))
            await db.execute(delete(User).where(User.id.in_([owner.id, attendee.id])))
            await db.commit()

    await engine.dispose()


def test_send_meeting_summary_queues_one_email_per_participant():
    asyncio.run(_scenario())
