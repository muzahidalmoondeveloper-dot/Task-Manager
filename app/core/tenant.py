import uuid
from dataclasses import dataclass

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_errors import AppException, AuthError, ErrorDef, TokenError
from app.core.database import get_db
from app.core.org_roles import APP_MANAGEMENT_ROLES, OWNER, ORG_MANAGEMENT_ROLES
from app.core.plan_limits import PlanLimits, get_plan_limits
from app.core.security import decode_access_token
from app.core.token_cache import TokenCache, get_token_cache
from app.models.organization import Organization, OrganizationMembership
from app.models.user import User
from app.repositories.user_repository import UserRepository

from fastapi import status as http_status

# ── Extra error definitions for tenant layer ─────────────────────────────────

_TOKEN_NO_ORG = ErrorDef(
    code="TOKEN_NO_ORG_CONTEXT",
    status=http_status.HTTP_401_UNAUTHORIZED,
    message="This token has no organization context. Call /auth/select-organization first.",
)
_ORG_NOT_FOUND = ErrorDef(
    code="ORG_NOT_FOUND",
    status=http_status.HTTP_404_NOT_FOUND,
    message="Organization not found.",
)
_ORG_INACTIVE = ErrorDef(
    code="ORG_INACTIVE",
    status=http_status.HTTP_403_FORBIDDEN,
    message="This organization is suspended.",
)
_NOT_ORG_MEMBER = ErrorDef(
    code="NOT_ORG_MEMBER",
    status=http_status.HTTP_403_FORBIDDEN,
    message="You are not a member of this organization.",
)
_PLAN_LIMIT_EXCEEDED = ErrorDef(
    code="PLAN_LIMIT_EXCEEDED",
    status=http_status.HTTP_402_PAYMENT_REQUIRED,
    message="Your plan limit has been reached. Please upgrade.",
)
_FEATURE_NOT_AVAILABLE = ErrorDef(
    code="FEATURE_NOT_AVAILABLE",
    status=http_status.HTTP_402_PAYMENT_REQUIRED,
    message="This feature is not available on your current plan.",
)
_ORG_ADMIN_REQUIRED = ErrorDef(
    code="ORG_ADMIN_REQUIRED",
    status=http_status.HTTP_403_FORBIDDEN,
    message="Organization admin or owner access required.",
)
_ORG_OWNER_REQUIRED = ErrorDef(
    code="ORG_OWNER_REQUIRED",
    status=http_status.HTTP_403_FORBIDDEN,
    message="Organization owner access required.",
)
_ORG_MANAGER_REQUIRED = ErrorDef(
    code="ORG_MANAGER_REQUIRED",
    status=http_status.HTTP_403_FORBIDDEN,
    message="Team manager, admin, or owner access required.",
)

bearer_scheme = HTTPBearer(auto_error=False)


@dataclass
class TenantContext:
    organization_id: uuid.UUID
    organization: Organization
    membership: OrganizationMembership
    user: User
    db: AsyncSession

    @property
    def plan_limits(self) -> PlanLimits:
        return get_plan_limits(self.organization.plan)

    @property
    def org_role(self) -> str:
        return self.membership.role

    @property
    def is_owner(self) -> bool:
        return self.membership.role == OWNER

    @property
    def is_admin_or_owner(self) -> bool:
        """True for Owner and Admin — org-level administration."""
        return self.membership.role in ORG_MANAGEMENT_ROLES

    @property
    def is_manager_or_above(self) -> bool:
        """True for Owner, Admin, and Team Manager — app-level management."""
        return self.membership.role in APP_MANAGEMENT_ROLES


async def get_tenant_context(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
    token_cache: TokenCache = Depends(get_token_cache),
) -> TenantContext:
    if credentials is None:
        raise TokenError.invalid("Authentication credentials were not provided.")

    payload = decode_access_token(credentials.credentials)
    if payload is None:
        raise TokenError.invalid()

    jti = payload.get("jti")
    if jti and await token_cache.is_access_token_blacklisted(jti):
        raise TokenError.invalid("Token has been revoked.")

    # ── Load user ────────────────────────────────────────────────────────────
    user_id_raw = payload.get("sub")
    if not user_id_raw:
        raise TokenError.invalid()

    try:
        user_id = int(user_id_raw)
    except (ValueError, TypeError):
        raise TokenError.invalid()

    user_repo = UserRepository(db)
    user = await user_repo.get_by_id(user_id)
    if user is None:
        raise AuthError.user_not_found()
    if not user.is_active:
        raise AuthError.account_inactive()

    # ── Resolve org from JWT claim ────────────────────────────────────────────
    org_id_raw = payload.get("org_id")
    if not org_id_raw:
        raise AppException(_TOKEN_NO_ORG)

    try:
        org_id = uuid.UUID(str(org_id_raw))
    except (ValueError, AttributeError):
        raise TokenError.invalid("Invalid organization identifier in token.")

    # ── Load organization ─────────────────────────────────────────────────────
    result = await db.execute(select(Organization).where(Organization.id == org_id))
    organization = result.scalar_one_or_none()

    if organization is None:
        raise AppException(_ORG_NOT_FOUND)
    if not organization.is_active:
        raise AppException(_ORG_INACTIVE)

    # ── Verify membership ─────────────────────────────────────────────────────
    result = await db.execute(
        select(OrganizationMembership).where(
            OrganizationMembership.organization_id == org_id,
            OrganizationMembership.user_id == user_id,
            OrganizationMembership.is_active.is_(True),
        )
    )
    membership = result.scalar_one_or_none()

    if membership is None:
        raise AppException(_NOT_ORG_MEMBER)

    return TenantContext(
        organization_id=org_id,
        organization=organization,
        membership=membership,
        user=user,
        db=db,
    )


# ── Role-gated convenience dependencies ──────────────────────────────────────

async def require_org_admin(
    tenant: TenantContext = Depends(get_tenant_context),
) -> TenantContext:
    """Owner or Admin only — org settings, billing, member management."""
    if not tenant.is_admin_or_owner:
        raise AppException(_ORG_ADMIN_REQUIRED)
    return tenant


async def require_org_owner(
    tenant: TenantContext = Depends(get_tenant_context),
) -> TenantContext:
    """Owner only — destructive org actions (delete, transfer)."""
    if not tenant.is_owner:
        raise AppException(_ORG_OWNER_REQUIRED)
    return tenant


async def require_org_manager(
    tenant: TenantContext = Depends(get_tenant_context),
) -> TenantContext:
    """Owner, Admin, or Team Manager — teams / projects / tasks mutations."""
    if not tenant.is_manager_or_above:
        raise AppException(_ORG_MANAGER_REQUIRED)
    return tenant


# ── Plan limit enforcement ────────────────────────────────────────────────────

async def enforce_member_limit(tenant: TenantContext, db: AsyncSession) -> None:
    limits = tenant.plan_limits
    if limits.max_members == -1:
        return
    result = await db.execute(
        select(OrganizationMembership).where(
            OrganizationMembership.organization_id == tenant.organization_id,
            OrganizationMembership.is_active.is_(True),
        )
    )
    count = len(result.scalars().all())
    if count >= limits.max_members:
        raise AppException(
            _PLAN_LIMIT_EXCEEDED,
            message=f"Your plan allows a maximum of {limits.max_members} members.",
            details={"limit": limits.max_members, "current": count, "resource": "members"},
        )


def enforce_feature(tenant: TenantContext, feature: str) -> None:
    """Raise if the org's plan does not include the given feature flag."""
    limits = tenant.plan_limits
    if not getattr(limits, feature, False):
        raise AppException(
            _FEATURE_NOT_AVAILABLE,
            details={"feature": feature, "current_plan": tenant.organization.plan},
        )
