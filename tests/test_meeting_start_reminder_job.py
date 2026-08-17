"""Regression test for the meeting-start reminder scheduler job
(app.services.automation_scheduler.run_meeting_start_reminders): once a
scheduled meeting's start time arrives, every assigned attendee gets a
"meeting_reminder" notification exactly once — a meeting whose time hasn't
arrived yet, or one that's already been reminded, is left alone.

Runs against the real database. Every row this test creates is deleted
before it returns.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete, select

from app.core.database import AsyncSessionLocal, engine
from app.models.meeting import Meeting, MeetingParticipant
from app.models.notification import Notification
from app.models.organization import Organization, OrganizationMembership
from app.models.user import User
from app.services.automation_scheduler import run_meeting_start_reminders


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="Reminder Owner", email=f"reminder.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        attendee = User(full_name="Reminder Attendee", email=f"reminder.attendee.{suffix}@test.invalid", hashed_password="x", role="team_member")
        db.add_all([owner, attendee])
        await db.flush()

        org = Organization(name=f"Reminder Org {suffix}", slug=f"reminder-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.flush()

        db.add_all([
            OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner"),
            OrganizationMembership(organization_id=org.id, user_id=attendee.id, role="team_member"),
        ])
        await db.flush()

        now = datetime.now(timezone.utc)

        # Due now — should get reminded.
        due_meeting = Meeting(title=f"Due Meeting {suffix}", organization_id=org.id, scheduled_at=now - timedelta(minutes=1), status="scheduled", organizer_id=owner.id)
        # Not due yet — should NOT get reminded.
        future_meeting = Meeting(title=f"Future Meeting {suffix}", organization_id=org.id, scheduled_at=now + timedelta(hours=1), status="scheduled", organizer_id=owner.id)
        # Already reminded — should NOT get reminded again.
        already_meeting = Meeting(title=f"Already Reminded Meeting {suffix}", organization_id=org.id, scheduled_at=now - timedelta(minutes=2), status="scheduled", organizer_id=owner.id, reminder_sent_at=now - timedelta(seconds=30))
        db.add_all([due_meeting, future_meeting, already_meeting])
        await db.flush()

        db.add_all([
            MeetingParticipant(meeting_id=due_meeting.id, user_id=attendee.id),
            MeetingParticipant(meeting_id=future_meeting.id, user_id=attendee.id),
            MeetingParticipant(meeting_id=already_meeting.id, user_id=attendee.id),
        ])
        await db.commit()

        meeting_ids = [due_meeting.id, future_meeting.id, already_meeting.id]

        try:
            await run_meeting_start_reminders()

            async with AsyncSessionLocal() as check_db:
                result = await check_db.execute(
                    select(Notification).where(
                        Notification.user_id == attendee.id,
                        Notification.type == "meeting_reminder",
                        Notification.meeting_id.in_(meeting_ids),
                    )
                )
                reminders = list(result.scalars().all())
                assert [n.meeting_id for n in reminders] == [due_meeting.id], (
                    "only the meeting whose scheduled time has arrived and hasn't been reminded yet should fire"
                )

                # Running the job again right away must not double-send —
                # reminder_sent_at is now set on due_meeting.
                await run_meeting_start_reminders()
                result2 = await check_db.execute(
                    select(Notification).where(
                        Notification.user_id == attendee.id,
                        Notification.type == "meeting_reminder",
                        Notification.meeting_id == due_meeting.id,
                    )
                )
                assert len(list(result2.scalars().all())) == 1, "the job must not re-notify once reminder_sent_at is set"
        finally:
            async with AsyncSessionLocal() as cleanup_db:
                await cleanup_db.execute(delete(Notification).where(Notification.meeting_id.in_(meeting_ids)))
                await cleanup_db.execute(delete(MeetingParticipant).where(MeetingParticipant.meeting_id.in_(meeting_ids)))
                await cleanup_db.execute(delete(Meeting).where(Meeting.id.in_(meeting_ids)))
                await cleanup_db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
                await cleanup_db.execute(delete(Organization).where(Organization.id == org.id))
                await cleanup_db.execute(delete(User).where(User.id.in_([owner.id, attendee.id])))
                await cleanup_db.commit()

    await engine.dispose()


def test_meeting_start_reminder_fires_once_when_due():
    asyncio.run(_scenario())
