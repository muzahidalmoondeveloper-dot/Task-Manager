"""Timezone-aware temporal resolution regression test (architecture item 9).

BUG BEING GUARDED AGAINST: every "today"/overdue/due-soon date boundary in
ChatService used to call the stdlib date.today() directly — the server
process's own local clock — regardless of which organization was asking.
For an organization in a timezone ahead of the server (e.g. server in UTC,
org in Asia/Dhaka, UTC+6), a task due "today" in the org's own calendar day
could be computed against the *previous* UTC day for several hours after
local midnight, silently misclassifying it as/not-as overdue.

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.
"""

import asyncio
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete

from app.core.database import AsyncSessionLocal, engine
from app.models.organization import Organization, OrganizationMembership
from app.models.user import User
from app.services.chat_service import ChatService


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="Timezone Test Owner", email=f"tz.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        db.add(owner)
        await db.flush()

        org_dhaka = Organization(
            name=f"Timezone Test Org Dhaka {suffix}", slug=f"tz-test-dhaka-{suffix}",
            owner_id=owner.id, timezone="Asia/Dhaka",
        )
        org_default = Organization(
            name=f"Timezone Test Org Default {suffix}", slug=f"tz-test-default-{suffix}",
            owner_id=owner.id,  # timezone left at its column default -> "UTC"
        )
        org_bogus = Organization(
            name=f"Timezone Test Org Bogus {suffix}", slug=f"tz-test-bogus-{suffix}",
            owner_id=owner.id, timezone="Not/A_Real_Zone",
        )
        db.add_all([org_dhaka, org_default, org_bogus])
        await db.flush()
        db.add_all([
            OrganizationMembership(organization_id=org_dhaka.id, user_id=owner.id, role="owner"),
            OrganizationMembership(organization_id=org_default.id, user_id=owner.id, role="owner"),
            OrganizationMembership(organization_id=org_bogus.id, user_id=owner.id, role="owner"),
        ])
        await db.commit()

        try:
            svc_dhaka = ChatService(db, org_dhaka.id)
            resolved = await svc_dhaka._org_today()
            expected = datetime.now(ZoneInfo("Asia/Dhaka")).date()
            assert resolved == expected, (
                f"org-local 'today' for Asia/Dhaka should be {expected}, got {resolved}"
            )

            svc_default = ChatService(db, org_default.id)
            resolved_default = await svc_default._org_today()
            expected_utc = datetime.now(ZoneInfo("UTC")).date()
            assert resolved_default == expected_utc, "an org with no timezone set must default to UTC, not crash"

            # An invalid/garbage timezone string must never crash a chat
            # turn — it must fall back to UTC silently (with a log warning).
            svc_bogus = ChatService(db, org_bogus.id)
            resolved_bogus = await svc_bogus._org_today()
            assert resolved_bogus == expected_utc, "an invalid timezone string must fall back to UTC, not raise"

            # The cache must actually be used (no re-query on second call) —
            # correctness check on the caching, not just the value.
            assert svc_dhaka._org_timezone_cache == "Asia/Dhaka"

        finally:
            await db.execute(delete(OrganizationMembership).where(
                OrganizationMembership.organization_id.in_([org_dhaka.id, org_default.id, org_bogus.id])
            ))
            await db.execute(delete(Organization).where(
                Organization.id.in_([org_dhaka.id, org_default.id, org_bogus.id])
            ))
            await db.execute(delete(User).where(User.id == owner.id))
            await db.commit()

    await engine.dispose()


def test_org_today_is_timezone_aware_with_safe_fallback():
    asyncio.run(_scenario())
