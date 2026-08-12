"""Organization timezone configurability regression test (strict acceptance
audit gap #5 — "Organization.timezone is not configurable by any
user-facing path").

BUG BEING GUARDED AGAINST: the `timezone` column and `_org_today()` logic
were correct, but `OrganizationUpdate` (the schema backing `PUT
/organizations/current`) had no `timezone` field at all — there was no way
for any admin, through any API call, to ever set an org's timezone away
from its "UTC" default. This test drives the same repository update path
the route uses and proves: (1) `timezone` is now a real, validated field on
`OrganizationUpdate` — an invalid IANA name is rejected; (2) a valid update
actually persists and is immediately reflected by `ChatService._org_today()`,
closing the loop between the settings API and the AI Copilot's temporal
resolution.

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.
"""

import asyncio
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from pydantic import ValidationError
from sqlalchemy import delete

from app.core.database import AsyncSessionLocal, engine
from app.models.organization import Organization, OrganizationMembership
from app.models.user import User
from app.repositories.organization_repository import OrganizationRepository
from app.schemas.organization import OrganizationUpdate
from app.services.chat_service import ChatService


def test_organization_update_schema_rejects_invalid_timezone():
    raised = False
    try:
        OrganizationUpdate(timezone="Not/A_Real_Zone")
    except ValidationError:
        raised = True
    assert raised, "an invalid IANA timezone name must be rejected at the API boundary, not silently accepted"


def test_organization_update_schema_accepts_valid_timezone():
    update = OrganizationUpdate(timezone="Asia/Dhaka")
    assert update.timezone == "Asia/Dhaka"


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="TZConfig Owner", email=f"tzconfig.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        db.add(owner)
        await db.flush()

        org = Organization(name=f"TZConfig Org {suffix}", slug=f"tzconfig-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.flush()
        db.add(OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner"))
        await db.commit()
        org_id = org.id

        try:
            # ── Starts at the column default ──
            svc_before = ChatService(db, org_id)
            assert await svc_before._org_today() == datetime.now(ZoneInfo("UTC")).date()

            # ── Same update path PUT /organizations/current uses ──
            repo = OrganizationRepository(db)
            update = OrganizationUpdate(timezone="Asia/Dhaka")
            await repo.update(org, update.model_dump(exclude_unset=True))
            await db.commit()

            # ── A fresh ChatService (new request) immediately reflects it ──
            svc_after = ChatService(db, org_id)
            assert await svc_after._org_today() == datetime.now(ZoneInfo("Asia/Dhaka")).date(), (
                "TIMEZONE CONFIG BUG: an org-settings update to timezone must be immediately reflected by _org_today()"
            )

        finally:
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org_id))
            await db.execute(delete(Organization).where(Organization.id == org_id))
            await db.execute(delete(User).where(User.id == owner.id))
            await db.commit()

    await engine.dispose()


def test_organization_timezone_update_is_reflected_by_org_today():
    asyncio.run(_scenario())
