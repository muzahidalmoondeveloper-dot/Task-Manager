"""Regression test: only Owner/Admin, Team Manager, or Project Manager (role
or granted flag) may create a meeting — a plain Team Member must be refused.

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from fastapi import HTTPException
from sqlalchemy import delete

from app.api.routes.meetings import create_meeting
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import PROJECT_MANAGER
from app.core.tenant import TenantContext
from app.models.meeting import Meeting
from app.models.organization import Organization, OrganizationMembership
from app.models.user import User
from app.schemas.meeting import MeetingCreate


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="MeetingPerm Owner", email=f"meetingperm.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        member = User(full_name="MeetingPerm Member", email=f"meetingperm.member.{suffix}@test.invalid", hashed_password="x", role="team_member")
        hybrid_pm = User(full_name="MeetingPerm PM", email=f"meetingperm.pm.{suffix}@test.invalid", hashed_password="x", role=PROJECT_MANAGER)
        db.add_all([owner, member, hybrid_pm])
        await db.flush()

        org = Organization(name=f"MeetingPerm Org {suffix}", slug=f"meetingperm-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.flush()

        owner_membership = OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner")
        member_membership = OrganizationMembership(organization_id=org.id, user_id=member.id, role="team_member")
        pm_membership = OrganizationMembership(organization_id=org.id, user_id=hybrid_pm.id, role=PROJECT_MANAGER)
        db.add_all([owner_membership, member_membership, pm_membership])
        await db.commit()

        when = datetime.now(timezone.utc) + timedelta(days=3)
        member_tenant = TenantContext(organization_id=org.id, organization=org, membership=member_membership, user=member, db=db)
        pm_tenant = TenantContext(organization_id=org.id, organization=org, membership=pm_membership, user=hybrid_pm, db=db)

        created_ids = []
        try:
            # Plain team member: refused.
            try:
                await create_meeting(
                    MeetingCreate(title=f"Should Never Exist {suffix}", scheduled_at=when),
                    db=db, tenant=member_tenant,
                )
                raise AssertionError("a plain team member must not be able to create a meeting")
            except HTTPException as exc:
                assert exc.status_code == 403

            # Project Manager: allowed.
            meeting = await create_meeting(
                MeetingCreate(title=f"PM Created Meeting {suffix}", scheduled_at=when),
                db=db, tenant=pm_tenant,
            )
            created_ids.append(meeting.id)
            assert meeting.title == f"PM Created Meeting {suffix}"

        finally:
            if created_ids:
                await db.execute(delete(Meeting).where(Meeting.id.in_(created_ids)))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id == org.id))
            await db.execute(delete(User).where(User.id.in_([owner.id, member.id, hybrid_pm.id])))
            await db.commit()

    await engine.dispose()


def test_only_managers_and_project_managers_can_create_meetings():
    asyncio.run(_scenario())
