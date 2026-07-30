from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import status


@dataclass(frozen=True, slots=True)
class ErrorDef:
    code: str
    status: int
    message: str


class AppException(Exception):
    def __init__(
        self,
        error: ErrorDef,
        *,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        self.code = error.code
        self.status_code = error.status
        self.message = message or error.message
        self.details = details
        self.timestamp = datetime.now(UTC).isoformat()
        super().__init__(self.message)

    def to_dict(self, request_id: str | None = None) -> dict[str, Any]:
        error_dict: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "timestamp": self.timestamp,
        }
        if self.details:
            error_dict["details"] = self.details
        if request_id:
            error_dict["request_id"] = request_id
        return {"error": error_dict}


# ── Error definitions ────────────────────────────────────────────────────────

_INVALID_CREDENTIALS = ErrorDef(
    code="AUTH_INVALID_CREDENTIALS",
    status=status.HTTP_401_UNAUTHORIZED,
    message="Invalid email or password.",
)
_RATE_LIMITED = ErrorDef(
    code="AUTH_RATE_LIMITED",
    status=status.HTTP_429_TOO_MANY_REQUESTS,
    message="Too many attempts. Please try again later.",
)
_EMAIL_EXISTS = ErrorDef(
    code="AUTH_EMAIL_EXISTS",
    status=status.HTTP_400_BAD_REQUEST,
    message="Email already registered.",
)
_INVALID_PASSWORD = ErrorDef(
    code="AUTH_INVALID_PASSWORD",
    status=status.HTTP_400_BAD_REQUEST,
    message="Password does not meet requirements.",
)
_USER_NOT_FOUND = ErrorDef(
    code="AUTH_USER_NOT_FOUND",
    status=status.HTTP_404_NOT_FOUND,
    message="User not found.",
)
_INVALID_OTP = ErrorDef(
    code="AUTH_INVALID_OTP",
    status=status.HTTP_400_BAD_REQUEST,
    message="Invalid or expired OTP.",
)
_ACCOUNT_INACTIVE = ErrorDef(
    code="AUTH_ACCOUNT_INACTIVE",
    status=status.HTTP_403_FORBIDDEN,
    message="User account is inactive.",
)
_EMAIL_NOT_VERIFIED = ErrorDef(
    code="AUTH_EMAIL_NOT_VERIFIED",
    status=status.HTTP_403_FORBIDDEN,
    message="Email is not verified yet.",
)
_EMAIL_ALREADY_VERIFIED = ErrorDef(
    code="AUTH_EMAIL_ALREADY_VERIFIED",
    status=status.HTTP_400_BAD_REQUEST,
    message="Email is already verified.",
)
_OTP_PURPOSE_INVALID = ErrorDef(
    code="AUTH_OTP_PURPOSE_INVALID",
    status=status.HTTP_400_BAD_REQUEST,
    message="Invalid OTP purpose.",
)
_TOKEN_INVALID = ErrorDef(
    code="TOKEN_INVALID",
    status=status.HTTP_401_UNAUTHORIZED,
    message="Token is invalid.",
)
_TOKEN_EXPIRED = ErrorDef(
    code="TOKEN_EXPIRED",
    status=status.HTTP_401_UNAUTHORIZED,
    message="Token has expired.",
)
_TOKEN_REVOKED = ErrorDef(
    code="TOKEN_REVOKED",
    status=status.HTTP_401_UNAUTHORIZED,
    message="Token has been revoked.",
)


# ── Typed error factories ────────────────────────────────────────────────────

class AuthError:
    @staticmethod
    def invalid_credentials() -> AppException:
        return AppException(_INVALID_CREDENTIALS)

    @staticmethod
    def rate_limited(message: str | None = None) -> AppException:
        return AppException(_RATE_LIMITED, message=message)

    @staticmethod
    def email_exists() -> AppException:
        return AppException(_EMAIL_EXISTS)

    @staticmethod
    def invalid_password(errors: list[str]) -> AppException:
        return AppException(_INVALID_PASSWORD, details={"errors": errors})

    @staticmethod
    def user_not_found() -> AppException:
        return AppException(_USER_NOT_FOUND)

    @staticmethod
    def invalid_otp() -> AppException:
        return AppException(_INVALID_OTP)

    @staticmethod
    def otp_expired() -> AppException:
        return AppException(_INVALID_OTP, message="OTP has expired.")

    @staticmethod
    def account_inactive() -> AppException:
        return AppException(_ACCOUNT_INACTIVE)

    @staticmethod
    def email_not_verified() -> AppException:
        return AppException(_EMAIL_NOT_VERIFIED)

    @staticmethod
    def email_already_verified() -> AppException:
        return AppException(_EMAIL_ALREADY_VERIFIED)

    @staticmethod
    def otp_purpose_invalid() -> AppException:
        return AppException(_OTP_PURPOSE_INVALID)


class TokenError:
    @staticmethod
    def invalid(message: str | None = None) -> AppException:
        return AppException(_TOKEN_INVALID, message=message)

    @staticmethod
    def expired() -> AppException:
        return AppException(_TOKEN_EXPIRED)

    @staticmethod
    def revoked(message: str | None = None) -> AppException:
        return AppException(
            _TOKEN_REVOKED,
            message=message or "Token has been revoked. All sessions terminated for security.",
        )
