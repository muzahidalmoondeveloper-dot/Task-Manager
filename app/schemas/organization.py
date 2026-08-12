import re
import uuid
from datetime import date, datetime, timezone

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

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
    plan: str = Field(default="starter")
    billing_interval: str = Field(default="monthly")

    @field_validator("plan")
    @classmethod
    def validate_plan(cls, v: str) -> str:
        if v not in ALL_PLANS:
            raise ValueError(f"Plan must be one of: {', '.join(ALL_PLANS)}")
        return v

    @field_validator("billing_interval")
    @classmethod
    def validate_billing_interval(cls, v: str) -> str:
        if v not in ("monthly", "annual"):
            raise ValueError("billing_interval must be 'monthly' or 'annual'")
        return v

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
    # Architecture item 9 — timezone-aware temporal resolution (strict
    # acceptance audit gap #5): previously a real DB column with correct
    # _org_today() logic behind it, but with no way for any org admin to
    # actually set it away from the default "UTC" through any API or UI
    # path, making the feature functionally inert. Validated against the
    # IANA tz database at the API boundary (not just accepted as any
    # string) so a typo can't silently break every future "today"
    # computation for the org.
    timezone: str | None = None

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise ValueError(f"Unknown timezone: {value!r}. Use an IANA name, e.g. 'Asia/Dhaka' or 'America/New_York'.")
        return value


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
    timezone: str = "UTC"
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class OrganizationSetupComplete(BaseModel):
    """Body for PUT /organizations/{id}/setup — final step of the creation wizard."""
    name: str | None = Field(default=None, min_length=2, max_length=255)
    logo_url: str | None = None
    website: str | None = None
    industry: str | None = None


class ScoreboardWeightsRead(BaseModel):
    scoreboard_completion_weight: float
    scoreboard_on_time_weight: float
    scoreboard_overdue_weight: float

    model_config = {"from_attributes": True}


class ScoreboardWeightsUpdate(BaseModel):
    scoreboard_completion_weight: float = Field(ge=0, le=1)
    scoreboard_on_time_weight: float = Field(ge=0, le=1)
    scoreboard_overdue_weight: float = Field(ge=0, le=1)

    @field_validator("scoreboard_overdue_weight")
    @classmethod
    def validate_sum(cls, v, info):
        completion = info.data.get("scoreboard_completion_weight")
        on_time = info.data.get("scoreboard_on_time_weight")
        if completion is None or on_time is None:
            return v
        total = completion + on_time + v
        if abs(total - 1.0) > 0.01:
            raise ValueError(f"Weights must sum to 100% (got {round(total * 100, 1)}%).")
        return v


class SlugCheckResponse(BaseModel):
    slug: str
    available: bool


class OrgSummary(BaseModel):
    """Lightweight org info for org lists and the switcher dropdown."""
    id: uuid.UUID
    name: str
    slug: str
    plan: str
    logo_url: str | None = None
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
    is_org_admin: bool = False
    is_team_manager: bool = False
    is_project_manager: bool = False
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


# ── Client invitations (shared invitation service) ─────────────────────────────

class _RefUser(BaseModel):
    id: int
    full_name: str | None = None
    email: str

    model_config = {"from_attributes": True}


class _RefProject(BaseModel):
    id: int
    name: str

    model_config = {"from_attributes": True}


class _RefTemplate(BaseModel):
    id: int
    name: str

    model_config = {"from_attributes": True}


class ClientInvitationCreate(BaseModel):
    email: EmailStr
    project_id: int
    client_name: str | None = Field(default=None, max_length=255)
    company_name: str | None = Field(default=None, max_length=255)
    phone_number: str | None = Field(default=None, max_length=50)
    project_manager_id: int | None = None
    onboarding_template_id: int | None = None
    due_date: date | None = None
    message: str | None = None
    expires_in_days: int = Field(default=3, ge=1, le=30)
    save_as_draft: bool = False


class ClientInvitationRead(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    email: str
    client_name: str | None = None
    company_name: str | None = None
    phone_number: str | None = None
    role: str
    status: str
    message: str | None = None
    token: str
    project: _RefProject | None = None
    project_manager: _RefUser | None = None
    onboarding_template: _RefTemplate | None = None
    onboarding_id: int | None = None
    due_date: date | None = None
    invited_by: _RefUser
    expires_at: datetime
    accepted_at: datetime | None = None
    opened_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}

    @model_validator(mode="after")
    def compute_effective_status(self):
        if self.status in ("accepted", "revoked", "draft"):
            return self
        now = datetime.now(timezone.utc)
        expires_at = self.expires_at if self.expires_at.tzinfo else self.expires_at.replace(tzinfo=timezone.utc)
        if expires_at < now:
            self.status = "expired"
        return self


# ── Subscription & Usage ──────────────────────────────────────────────────────

class SubscriptionRead(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    plan: str
    status: str
    seats: int
    billing_interval: str
    trial_ends_at: datetime | None
    current_period_start: datetime | None
    current_period_end: datetime | None
    cancel_at_period_end: bool
    extra_teams: int
    extra_users: int
    stripe_subscription_id: str | None
    stripe_customer_id: str | None

    model_config = {"from_attributes": True}


class BillingStatusRead(BaseModel):
    status: str
    trial_ends_at: datetime | None
    is_locked: bool


class AddonUpdateRequest(BaseModel):
    extra_teams: int = Field(ge=0, le=500)
    extra_users: int = Field(ge=0, le=500)


class BillingPortalResponse(BaseModel):
    url: str


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
