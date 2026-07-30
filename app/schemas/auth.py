from pydantic import BaseModel, EmailStr, Field

from app.schemas.user import UserRead


# Avoid circular import — OrgSummary is a lightweight schema defined in organization.py
# and imported here only for the login response.
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from app.schemas.organization import OrgSummary


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    expires_at: int
    token_type: str = "bearer"
    user: UserRead
    # Status of the org now active in this token — lets the frontend decide
    # whether to resume the org-creation wizard ('pending_setup') or not.
    org_status: str | None = None
    # Populated only by org-creation — avoids a follow-up round trip for the new org's id.
    organization: dict | None = None


class AuthenticatedUserResponse(BaseModel):
    user: UserRead
    org_status: str | None = None


class VerifyRegisterOTPRequest(BaseModel):
    email: EmailStr
    otp_code: str = Field(min_length=6, max_length=6)


class VerifyLoginOTPRequest(BaseModel):
    email: EmailStr
    otp_code: str = Field(min_length=6, max_length=6)


class LoginPasswordResponse(BaseModel):
    """Returned by POST /login before any OTP step is complete."""
    otp_required: bool = False
    email_verification_required: bool = False
    message: str
    email: str | None = None
    # Populated only when login completes without OTP
    access_token: str | None = None
    refresh_token: str | None = None
    expires_at: int | None = None
    token_type: str = "bearer"
    user: UserRead | None = None
    org_status: str | None = None
    # Multi-org selection — populated when the user belongs to >1 org
    requires_org_selection: bool = False
    organizations: list = []  # list[OrgSummary] — typed as list to avoid circular import


class ResendOTPRequest(BaseModel):
    email: EmailStr
    purpose: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    email: EmailStr
    otp_code: str = Field(min_length=6, max_length=6)
    new_password: str = Field(min_length=8, max_length=128)


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str
    logout_all_devices: bool = False


class AcceptInvitationRequest(BaseModel):
    token: str


class RegisterAndAcceptInvitationRequest(BaseModel):
    token: str
    full_name: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=8, max_length=128)
