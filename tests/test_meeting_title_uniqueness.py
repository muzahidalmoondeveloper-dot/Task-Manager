"""Regression test for the Create Meeting page's duplicate-name guard:
GET /meetings/check-title backs the live inline validation, and POST
/meetings enforces the same rule authoritatively (case-insensitive, per
organization) so a race between the live check and submit can't slip a
duplicate title through.

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from fastapi import HTTPException
from sqlalchemy import delete

from app.api.routes.meetings import check_meeting_title, create_meeting
from app.core.database import AsyncSessionLocal, engine
from app.core.tenant import TenantContext
from app.models.meeting import Meeting
from app.models.organization import Organization, OrganizationMembership
from app.models.user import User
from app.schemas.meeting import MeetingCreate


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="MeetingTitle Owner", email=f"meetingtitle.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        db.add(owner)
        await db.flush()

        org = Organization(name=f"MeetingTitle Org {suffix}", slug=f"meetingtitle-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.flush()

        owner_membership = OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner")
        db.add(owner_membership)
        await db.commit()

        tenant = TenantContext(organization_id=org.id, organization=org, membership=owner_membership, user=owner, db=db)
        title = f"Weekly Meeting Pulse {suffix}"
        when = datetime.now(timezone.utc) + timedelta(days=7)

        created_id = None
        try:
            # Before creation: the name is free.
            result = await check_meeting_title(title=title, db=db, tenant=tenant)
            assert result == {"exists": False}

            meeting = await create_meeting(
                MeetingCreate(title=title, scheduled_at=when, meeting_type="level_10", recurrence="weekly"),
                db=db, tenant=tenant,
            )
            created_id = meeting.id

            # After creation: live check now reports it taken, case-insensitively.
            result = await check_meeting_title(title=title.upper(), db=db, tenant=tenant)
            assert result == {"exists": True}

            # The authoritative create-time check refuses the same title too.
            try:
                await create_meeting(
                    MeetingCreate(title=title, scheduled_at=when, meeting_type="level_10", recurrence="weekly"),
                    db=db, tenant=tenant,
                )
                raise AssertionError("creating a second meeting with the same title must be refused")
            except HTTPException as exc:
                assert exc.status_code == 409
                assert "exists in your organization" in exc.detail

        finally:
            if created_id is not None:
                await db.execute(delete(Meeting).where(Meeting.id == created_id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id == org.id))
            await db.execute(delete(User).where(User.id == owner.id))
            await db.commit()

    await engine.dispose()


def test_meeting_title_must_be_unique_per_organization():
    asyncio.run(_scenario())
