import re
import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.core.org_roles import ALL_ORG_ROLES, TEAM_MEMBER
from app.core.plan_limits import ALL_PLANS, PlanLimits
from app.schemas.user import UserRead


def _slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^\w\s-]", "", value)
    value = re.sub(r"[\s_]+", "-", value)
    value = re.sub(r"-+", "-", value)
    return value.strip("-")


# ── Organization ──────────────────────────────────────────────────────────────

class OrganizationCreate(BaseModel):
    name: str = Field(min_length=2, max_length=255)
    slug: str | None = Field(default=None, min_length=2, max_length=100)

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        return v.strip()

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, v: str | None) -> str | None:
        if v is None:
            return None
        slug = _slugify(v)
        if not re.match(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$", slug) or len(slug) < 2:
            raise ValueError("Slug must be lowercase letters, numbers, and hyphens only.")
        return slug

    def resolved_slug(self) -> str:
        return self.slug or _slugify(self.name)


class OrganizationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=255)
    description: str | None = None
    logo_url: str | None = None
    website: str | None = None
    industry: str | None = None


class OrganizationRead(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    description: str | None
    logo_url: str | None
    website: str | None = None
    industry: str | None = None
    plan: str
    status: str = "active"
    is_active: bool
    owner_id: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class OrganizationSetupComplete(BaseModel):
    """Body for PUT /organizations/{id}/setup — final step of the creation wizard."""
    name: str | None = Field(default=None, min_length=2, max_length=255)
    logo_url: str | None = None
    website: str | None = None
    industry: str | None = None


class SlugCheckResponse(BaseModel):
    slug: str
    available: bool


class OrgSummary(BaseModel):
    """Lightweight org info for org lists and the switcher dropdown."""
    id: uuid.UUID
    name: str
    slug: str
    plan: str
    role: str       # user's role in this org
    status: str = "active"
    is_current: bool = False  # True when this is the JWT-active org

    model_config = {"from_attributes": True}


# ── Membership ────────────────────────────────────────────────────────────────

class MembershipRead(BaseModel):
    id: int
    organization_id: uuid.UUID
    user_id: int
    role: str
    is_active: bool
    joined_at: datetime
    user: UserRead

    model_config = {"from_attributes": True}


class InviteMemberRequest(BaseModel):
    email: EmailStr
    role: str = TEAM_MEMBER

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        if v not in ALL_ORG_ROLES:
            raise ValueError(f"Role must be one of: {', '.join(ALL_ORG_ROLES)}")
        return v


class UpdateMemberRoleRequest(BaseModel):
    role: str

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        if v not in ALL_ORG_ROLES:
            raise ValueError(f"Role must be one of: {', '.join(ALL_ORG_ROLES)}")
        return v


# ── Invitation ────────────────────────────────────────────────────────────────

class InvitationRead(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    email: str
    role: str
    project_id: int | None = None
    expires_at: datetime
    created_at: datetime

    model_config = {"from_attributes": True}


class ClientInvitationRequest(BaseModel):
    email: EmailStr


class AcceptInvitationRequest(BaseModel):
    token: str


# ── Subscription & Usage ──────────────────────────────────────────────────────

class SubscriptionRead(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    plan: str
    status: str
    seats: int
    trial_ends_at: datetime | None
    current_period_start: datetime | None
    current_period_end: datetime | None
    stripe_subscription_id: str | None
    stripe_customer_id: str | None

    model_config = {"from_attributes": True}


class PlanLimitsRead(BaseModel):
    max_members: int
    max_teams: int
    max_projects: int
    max_tasks_per_month: int
    has_ai_features: bool
    has_integrations: bool
    has_api_access: bool
    storage_gb: int


class OrgUsageRead(BaseModel):
    plan: str
    limits: PlanLimitsRead
    current_members: int
    current_teams: int
    current_projects: int
