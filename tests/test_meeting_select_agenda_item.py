"""Regression test for clicking an agenda item in the live meeting sidebar
to jump straight to it (POST /meetings/{id}/agenda/{item_id}/select) —
distinct from the sequential "Next" button (/agenda/next), which also marks
the outgoing item done. Selecting must not mark anything done, and must
work in any order (forward or back).

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete

from app.api.routes.meetings import create_meeting, select_agenda_item
from app.core.database import AsyncSessionLocal, engine
from app.core.tenant import TenantContext
from app.models.meeting import Meeting
from app.models.organization import Organization, OrganizationMembership
from app.models.user import User
from app.schemas.meeting import AgendaItemCreate, MeetingCreate


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="AgendaSelect Owner", email=f"agendaselect.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        db.add(owner)
        await db.flush()

        org = Organization(name=f"AgendaSelect Org {suffix}", slug=f"agendaselect-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.flush()

        owner_membership = OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner")
        db.add(owner_membership)
        await db.commit()

        when = datetime.now(timezone.utc) + timedelta(days=2)
        tenant = TenantContext(organization_id=org.id, organization=org, membership=owner_membership, user=owner, db=db)

        meeting = await create_meeting(
            MeetingCreate(
                title=f"Agenda Select Test {suffix}", scheduled_at=when, meeting_type="level_10",
                agenda_items=[
                    AgendaItemCreate(title="Segue", duration_minutes=5, sort_order=0),
                    AgendaItemCreate(title="Scorecard", duration_minutes=5, sort_order=1),
                    AgendaItemCreate(title="IDS", duration_minutes=60, sort_order=2),
                ],
            ),
            db=db, tenant=tenant,
        )

        try:
            segue_id, scorecard_id, ids_id = (a.id for a in meeting.agenda_items)

            # Jump straight to the 3rd item, skipping the 1st and 2nd.
            updated = await select_agenda_item(meeting.id, ids_id, db=db, tenant=tenant)
            assert updated.current_agenda_item_id == ids_id
            assert all(a.status != "done" for a in updated.agenda_items), "selecting must not mark anything done"

            # Jump back to the 1st item — selection isn't a one-way sequence.
            updated = await select_agenda_item(meeting.id, segue_id, db=db, tenant=tenant)
            assert updated.current_agenda_item_id == segue_id
            assert all(a.status != "done" for a in updated.agenda_items)

        finally:
            await db.execute(delete(Meeting).where(Meeting.id == meeting.id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id == org.id))
            await db.execute(delete(User).where(User.id == owner.id))
            await db.commit()

    await engine.dispose()


def test_selecting_an_agenda_item_jumps_freely_without_marking_anything_done():
    asyncio.run(_scenario())
