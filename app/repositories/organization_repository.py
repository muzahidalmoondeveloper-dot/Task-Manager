import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.org_roles import OWNER, TEAM_MEMBER
from app.models.organization import (
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
    Subscription,
)
from app.models.user import User

INVITATION_EXPIRE_HOURS = 72


class OrganizationRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ── Organization CRUD ─────────────────────────────────────────────────────

    async def create(
        self,
        name: str,
        slug: str,
        owner_id: int,
        plan: str = "free",
        status: str = "active",
    ) -> Organization:
        org = Organization(
            id=uuid.uuid4(),
            name=name,
            slug=slug,
            plan=plan,
            status=status,
            is_active=True,
            owner_id=owner_id,
        )
        self.db.add(org)
        await self.db.flush()
        return org

    async def count_owned_orgs(self, user_id: int) -> int:
        """Number of active organizations this user owns — used for plan/creation limits."""
        result = await self.db.execute(
            select(func.count(Organization.id)).where(
                Organization.owner_id == user_id,
                Organization.is_active.is_(True),
            )
        )
        return result.scalar_one() or 0

    async def get_by_id(self, org_id: uuid.UUID) -> Organization | None:
        result = await self.db.execute(
            select(Organization).where(Organization.id == org_id)
        )
        return result.scalar_one_or_none()

    async def get_by_slug(self, slug: str) -> Organization | None:
        result = await self.db.execute(
            select(Organization).where(Organization.slug == slug.lower().strip())
        )
        return result.scalar_one_or_none()

    async def slug_exists(self, slug: str) -> bool:
        result = await self.db.execute(
            select(Organization.id).where(Organization.slug == slug.lower().strip())
        )
        return result.scalar_one_or_none() is not None

    async def update(self, org: Organization, data: dict) -> Organization:
        for key, value in data.items():
            setattr(org, key, value)
        await self.db.flush()
        return org

    async def deactivate(self, org: Organization) -> Organization:
        org.is_active = False
        await self.db.flush()
        return org

    # ── Membership ────────────────────────────────────────────────────────────

    async def list_by_user(self, user_id: int) -> list[tuple[Organization, OrganizationMembership]]:
        result = await self.db.execute(
            select(Organization, OrganizationMembership)
            .join(
                OrganizationMembership,
                OrganizationMembership.organization_id == Organization.id,
            )
            .where(
                OrganizationMembership.user_id == user_id,
                OrganizationMembership.is_active.is_(True),
                Organization.is_active.is_(True),
            )
            .order_by(OrganizationMembership.joined_at.asc())
        )
        return list(result.all())

    async def get_membership(
        self, org_id: uuid.UUID, user_id: int
    ) -> OrganizationMembership | None:
        result = await self.db.execute(
            select(OrganizationMembership).where(
                OrganizationMembership.organization_id == org_id,
                OrganizationMembership.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    async def add_member(
        self, org_id: uuid.UUID, user_id: int, role: str = TEAM_MEMBER
    ) -> OrganizationMembership:
        membership = OrganizationMembership(
            organization_id=org_id,
            user_id=user_id,
            role=role,
            is_active=True,
        )
        self.db.add(membership)
        await self.db.flush()
        return membership

    async def remove_member(self, membership: OrganizationMembership) -> None:
        membership.is_active = False
        await self.db.flush()

    async def update_member_role(
        self, membership: OrganizationMembership, role: str
    ) -> OrganizationMembership:
        membership.role = role
        await self.db.flush()
        return membership

    async def list_members(
        self, org_id: uuid.UUID
    ) -> list[tuple[OrganizationMembership, User]]:
        result = await self.db.execute(
            select(OrganizationMembership, User)
            .join(User, User.id == OrganizationMembership.user_id)
            .where(
                OrganizationMembership.organization_id == org_id,
                OrganizationMembership.is_active.is_(True),
            )
            .order_by(OrganizationMembership.joined_at.asc())
        )
        return list(result.all())

    async def count_active_members(self, org_id: uuid.UUID) -> int:
        result = await self.db.execute(
            select(func.count(OrganizationMembership.id)).where(
                OrganizationMembership.organization_id == org_id,
                OrganizationMembership.is_active.is_(True),
            )
        )
        return result.scalar_one() or 0

    # ── Invitations ───────────────────────────────────────────────────────────

    async def create_invitation(
        self,
        org_id: uuid.UUID,
        email: str,
        role: str,
        invited_by_id: int,
        project_id: int | None = None,
    ) -> OrganizationInvitation:
        # Invalidate any existing pending invitations for same email+org
        existing = await self.db.execute(
            select(OrganizationInvitation).where(
                OrganizationInvitation.organization_id == org_id,
                OrganizationInvitation.email == email.lower().strip(),
                OrganizationInvitation.accepted_at.is_(None),
            )
        )
        for old in existing.scalars().all():
            await self.db.delete(old)

        invitation = OrganizationInvitation(
            id=uuid.uuid4(),
            organization_id=org_id,
            email=email.lower().strip(),
            role=role,
            project_id=project_id,
            invited_by_id=invited_by_id,
            token=secrets.token_urlsafe(48),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=INVITATION_EXPIRE_HOURS),
        )
        self.db.add(invitation)
        await self.db.flush()
        return invitation

    async def get_invitation_by_token(self, token: str) -> OrganizationInvitation | None:
        result = await self.db.execute(
            select(OrganizationInvitation)
            .options(
                selectinload(OrganizationInvitation.organization),
                selectinload(OrganizationInvitation.invited_by),
            )
            .where(OrganizationInvitation.token == token)
        )
        return result.scalar_one_or_none()

    async def get_invitation_by_id(
        self, org_id: uuid.UUID, invitation_id: uuid.UUID
    ) -> OrganizationInvitation | None:
        result = await self.db.execute(
            select(OrganizationInvitation).where(
                OrganizationInvitation.id == invitation_id,
                OrganizationInvitation.organization_id == org_id,
            )
        )
        return result.scalar_one_or_none()

    async def accept_invitation(
        self, invitation: OrganizationInvitation
    ) -> OrganizationInvitation:
        invitation.accepted_at = datetime.now(timezone.utc)
        await self.db.flush()
        return invitation

    async def list_pending_invitations(
        self, org_id: uuid.UUID
    ) -> list[OrganizationInvitation]:
        now = datetime.now(timezone.utc)
        result = await self.db.execute(
            select(OrganizationInvitation).where(
                OrganizationInvitation.organization_id == org_id,
                OrganizationInvitation.accepted_at.is_(None),
                OrganizationInvitation.expires_at > now,
            )
        )
        return list(result.scalars().all())

    # ── Subscription ──────────────────────────────────────────────────────────

    async def get_subscription(self, org_id: uuid.UUID) -> Subscription | None:
        result = await self.db.execute(
            select(Subscription).where(Subscription.organization_id == org_id)
        )
        return result.scalar_one_or_none()

    async def get_or_create_subscription(
        self, org_id: uuid.UUID, plan: str = "free"
    ) -> Subscription:
        sub = await self.get_subscription(org_id)
        if sub:
            return sub
        sub = Subscription(
            id=uuid.uuid4(),
            organization_id=org_id,
            plan=plan,
            status="active",
            seats=5,
        )
        self.db.add(sub)
        await self.db.flush()
        return sub
