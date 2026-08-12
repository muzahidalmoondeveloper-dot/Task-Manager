import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.organization import OrganizationMembership
from app.models.user import User
from app.schemas.user import UserCreate, UserUpdate


class UserRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_id(self, user_id: int) -> User | None:
        statement = select(User).where(User.id == user_id)
        result = await self.db.execute(statement)
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> User | None:
        normalized_email = email.lower().strip()
        statement = select(User).where(User.email == normalized_email)
        result = await self.db.execute(statement)
        return result.scalar_one_or_none()

    async def create(self, user_data: UserCreate) -> User:
        user = User(
            full_name=user_data.full_name.strip(),
            email=user_data.email.lower().strip(),
            hashed_password=hash_password(user_data.password),
            role=user_data.role,
            is_active=True,
        )

        self.db.add(user)
        await self.db.commit()
        await self.db.refresh(user)

        return user

    async def update(self, user: User, payload: UserUpdate) -> User:
        data = payload.model_dump(exclude_unset=True)

        password = data.pop("password", None)

        if "email" in data and data["email"]:
            data["email"] = data["email"].lower().strip()

        if "full_name" in data and data["full_name"]:
            data["full_name"] = data["full_name"].strip()

        for key, value in data.items():
            setattr(user, key, value)

        if password:
            user.hashed_password = hash_password(password)

        await self.db.commit()
        await self.db.refresh(user)

        return user

    async def list_all(self) -> list[User]:
        statement = select(User).order_by(User.created_at.desc())
        result = await self.db.execute(statement)
        return list(result.scalars().all())

    async def list_by_roles(self, roles: list[str]) -> list[User]:
        statement = (
            select(User)
            .where(User.role.in_(roles))
            .where(User.is_active.is_(True))
            .order_by(User.full_name.asc())
        )
        result = await self.db.execute(statement)
        return list(result.scalars().all())

    async def delete(self, user: User) -> None:
        await self.db.delete(user)
        await self.db.commit()

    async def list_by_org(self, org_id: uuid.UUID) -> list[User]:
        """Return all active users who are members of the given organization."""
        stmt = (
            select(User)
            .join(OrganizationMembership, OrganizationMembership.user_id == User.id)
            .where(
                OrganizationMembership.organization_id == org_id,
                OrganizationMembership.is_active.is_(True),
                User.is_active.is_(True),
            )
            .order_by(User.full_name.asc())
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().unique().all())

    async def list_by_org_all(self, org_id: uuid.UUID) -> list[tuple[User, OrganizationMembership]]:
        """Org-scoped, but INCLUDING inactive members — unlike list_by_org()
        (active-only, used for assignee resolution), this is for reporting
        that needs an accurate active/inactive breakdown for the org
        (e.g. "how many users do we have" including deactivated ones).
        Returns (User, OrganizationMembership) pairs — callers that need a
        role label should read membership.role, not the legacy/global
        User.role column, which can diverge from a user's actual role
        within THIS organization (e.g. a user who is "client" here but
        defaults to "team_member" globally)."""
        stmt = (
            select(User, OrganizationMembership)
            .join(OrganizationMembership, OrganizationMembership.user_id == User.id)
            .where(OrganizationMembership.organization_id == org_id)
            .order_by(User.full_name.asc())
        )
        result = await self.db.execute(stmt)
        return list(result.all())

    async def list_by_org_and_roles(self, org_id: uuid.UUID, roles: list[str]) -> list[User]:
        """Org-scoped counterpart to list_by_roles() — matches on the
        user's ORGANIZATION-scoped role (OrganizationMembership.role), not
        the legacy User.role column, since org membership is what actually
        governs a user's role within a given tenant."""
        stmt = (
            select(User)
            .join(OrganizationMembership, OrganizationMembership.user_id == User.id)
            .where(
                OrganizationMembership.organization_id == org_id,
                OrganizationMembership.role.in_(roles),
                OrganizationMembership.is_active.is_(True),
                User.is_active.is_(True),
            )
            .order_by(User.full_name.asc())
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().unique().all())