from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.core.org_roles import ADMIN, OWNER, PROJECT_MANAGER, TEAM_MANAGER, TEAM_MEMBER


USER_ROLES = {ADMIN, PROJECT_MANAGER, TEAM_MANAGER, TEAM_MEMBER}
# UserUpdate accepts OWNER too (the edit form always resends the target
# user's current role) — the route itself is what actually enforces that
# ownership can't be granted/revoked through this generic endpoint.
_UPDATE_ROLES = USER_ROLES | {OWNER}


class UserCreate(BaseModel):
    full_name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    role: str = TEAM_MEMBER

    # Used only when role == team_manager
    managed_team_id: int | None = None

    @field_validator("full_name")
    @classmethod
    def validate_full_name(cls, value: str) -> str:
        return value.strip()

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: EmailStr) -> str:
        return str(value).lower().strip()

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str) -> str:
        if value not in USER_ROLES:
            raise ValueError("Invalid user role.")
        return value


class UserUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=255)
    email: EmailStr | None = None
    password: str | None = Field(default=None, min_length=8, max_length=128)
    role: str | None = None
    is_active: bool | None = None
    # Additive admin privileges layered on top of `role` (e.g. a team_manager
    # or project_manager who's also been granted admin access).
    is_org_admin: bool | None = None
    # Additive Team Manager / Project Manager privileges layered on top of
    # `role` (e.g. a project_manager who's also been granted team manager
    # access, or vice versa) — the user keeps their primary role.
    is_team_manager: bool | None = None
    is_project_manager: bool | None = None

    # Used only when role == team_manager
    managed_team_id: int | None = None

    @field_validator("full_name")
    @classmethod
    def validate_full_name(cls, value: str | None) -> str | None:
        if value is None:
            return value
        return value.strip()

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: EmailStr | None) -> str | None:
        if value is None:
            return value
        return str(value).lower().strip()

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str | None) -> str | None:
        if value is not None and value not in _UPDATE_ROLES:
            raise ValueError("Invalid user role.")
        return value


class SelfProfileUpdate(BaseModel):
    """Fields a user may edit on their own profile — deliberately excludes
    role/is_active/is_org_admin/etc., which stay admin-only via UserUpdate."""
    full_name: str | None = Field(default=None, min_length=1, max_length=255)
    email: EmailStr | None = None

    @field_validator("full_name")
    @classmethod
    def validate_full_name(cls, value: str | None) -> str | None:
        if value is None:
            return value
        return value.strip()

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: EmailStr | None) -> str | None:
        if value is None:
            return value
        return str(value).lower().strip()


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


class UserRead(BaseModel):
    id: int
    full_name: str
    email: str
    role: str
    is_org_admin: bool = False
    is_team_manager: bool = False
    is_project_manager: bool = False
    is_active: bool

    email_verified_at: datetime | None = None
    last_login_otp_verified_at: datetime | None = None

    created_at: datetime
    updated_at: datetime

    model_config = {
        "from_attributes": True,
    }