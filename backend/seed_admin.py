import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.core.org_roles import ADMIN, ORG_OWNER
from app.models.organization import Organization, OrganizationMembership, Subscription
from app.repositories.user_repository import UserRepository
from app.schemas.user import UserCreate

logger = logging.getLogger(__name__)

DEFAULT_ORG_SLUG = "default"


async def _ensure_default_org(db, admin_id: int) -> None:
    """Create (or re-verify) the default organization for the seeded admin."""
    result = await db.execute(
        select(Organization).where(Organization.slug == DEFAULT_ORG_SLUG)
    )
    org = result.scalar_one_or_none()

    if org is None:
        import uuid
        org = Organization(
            id=uuid.uuid4(),
            name="Default Organization",
            slug=DEFAULT_ORG_SLUG,
            plan="professional",
            is_active=True,
            owner_id=admin_id,
        )
        db.add(org)
        await db.flush()
        logger.info("Default organization created (slug=%s)", DEFAULT_ORG_SLUG)
    else:
        logger.info("Default organization already exists (slug=%s)", DEFAULT_ORG_SLUG)

    # Ensure the admin is an owner member
    membership_result = await db.execute(
        select(OrganizationMembership).where(
            OrganizationMembership.organization_id == org.id,
            OrganizationMembership.user_id == admin_id,
        )
    )
    membership = membership_result.scalar_one_or_none()
    if membership is None:
        db.add(OrganizationMembership(
            organization_id=org.id,
            user_id=admin_id,
            role=ORG_OWNER,
            is_active=True,
        ))
        logger.info("Admin added as owner of default organization.")

    # Ensure a subscription exists
    sub_result = await db.execute(
        select(Subscription).where(Subscription.organization_id == org.id)
    )
    sub = sub_result.scalar_one_or_none()
    if sub is None:
        import uuid as _uuid
        db.add(Subscription(
            id=_uuid.uuid4(),
            organization_id=org.id,
            plan="professional",
            status="active",
            seats=-1,
        ))
        logger.info("Default subscription created (plan=professional).")

    await db.commit()


async def seed_admin() -> None:
    settings = get_settings()

    if not settings.ADMIN_EMAIL or not settings.ADMIN_PASSWORD:
        logger.info("ADMIN_EMAIL / ADMIN_PASSWORD not set — skipping admin seed.")
        return

    async with AsyncSessionLocal() as db:
        user_repo = UserRepository(db)
        existing = await user_repo.get_by_email(settings.ADMIN_EMAIL)

        if existing:
            changed = False

            if existing.email_verified_at is None:
                existing.email_verified_at = datetime.now(timezone.utc)
                changed = True

            if existing.last_login_otp_verified_at is None:
                existing.last_login_otp_verified_at = datetime.now(timezone.utc)
                changed = True

            if not existing.is_active:
                existing.is_active = True
                changed = True

            if changed:
                await db.commit()
                logger.info("Existing admin account verified and activated.")
            else:
                logger.info("Admin account already exists and is active.")

            await _ensure_default_org(db, existing.id)
            return

        admin = await user_repo.create(
            UserCreate(
                full_name=settings.ADMIN_NAME,
                email=settings.ADMIN_EMAIL,
                password=settings.ADMIN_PASSWORD,
                role=ADMIN,
            )
        )

        admin.email_verified_at = datetime.now(timezone.utc)
        admin.last_login_otp_verified_at = datetime.now(timezone.utc)
        admin.is_active = True

        await db.commit()
        await db.refresh(admin)

        logger.info("Default admin created: %s", settings.ADMIN_EMAIL)

        await _ensure_default_org(db, admin.id)


if __name__ == "__main__":
    asyncio.run(seed_admin())
