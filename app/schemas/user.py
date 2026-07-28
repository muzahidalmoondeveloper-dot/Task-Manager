from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.core.org_roles import ADMIN, TEAM_MANAGER, TEAM_MEMBER


USER_ROLES = {ADMIN, TEAM_MANAGER, TEAM_MEMBER}


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
        if value is not None and value not in USER_ROLES:
            raise ValueError("Invalid user role.")
        return value


class UserRead(BaseModel):
    id: int
    full_name: str
    email: str
    role: str
    is_active: bool

    email_verified_at: datetime | None = None
    last_login_otp_verified_at: datetime | None = None

    created_at: datetime
    updated_at: datetime

    model_config = {
        "from_attributes": True,
    }