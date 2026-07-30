import hashlib
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import get_settings

settings = get_settings()

password_context = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto",
)

WEAK_PASSWORDS = {
    "password", "password123", "12345678", "qwerty123", "abc123456",
    "password1", "welcome123", "123456789", "qwertyuiop", "letmein",
    "admin123", "passw0rd",
}


# ── Password helpers ─────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return password_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return password_context.verify(plain_password, hashed_password)


def validate_password_strength(password: str) -> dict[str, Any]:
    """Return {valid, score, strength, errors} for the given password."""
    errors: list[str] = []
    score = 0
    length = len(password)

    rules = [
        (r"[a-z]", "Password must contain at least one lowercase letter", 20),
        (r"[A-Z]", "Password must contain at least one uppercase letter", 20),
        (r"[0-9]", "Password must contain at least one number", 20),
        (r"[^a-zA-Z0-9]", "Password must contain at least one special character", 15),
    ]

    if length < 8:
        errors.append("Password must be at least 8 characters")
    else:
        score += 20
        score += 10 if length >= 12 else 0
        score += 5 if length >= 16 else 0

    for pattern, msg, pts in rules:
        if re.search(pattern, password):
            score += pts
        else:
            errors.append(msg)

    if password.lower() in WEAK_PASSWORDS:
        errors.append("Password is too common and easily guessable")
        score = 0

    if re.search(r"(.)\1{3,}", password):
        errors.append("Password contains too many repeated characters")
        score -= 10

    if re.search(r"(0123|1234|2345|3456|4567|5678|6789|abcd|bcde|cdef)", password.lower()):
        errors.append("Password contains sequential characters")
        score -= 10

    score = max(0, min(100, score))
    strength = "strong" if score >= 80 else ("medium" if score >= 60 else "weak")

    return {"valid": len(errors) == 0, "score": score, "strength": strength, "errors": errors}


# ── Token helpers ────────────────────────────────────────────────────────────

def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_access_token(
    subject: str,
    expires_delta: timedelta | None = None,
    extra_claims: dict[str, Any] | None = None,
    org_id: "uuid.UUID | None" = None,
    org_role: str | None = None,
) -> tuple[str, str, int]:
    """Return (encoded_token, jti, exp_unix_timestamp).

    Pass org_id + org_role to issue an organization-scoped token.
    Omit both to issue a token with no org context (e.g. immediately after
    registration, before the user has selected or created an org).
    """
    if expires_delta is None:
        expires_delta = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)

    now = datetime.now(timezone.utc)
    expire = now + expires_delta
    exp_unix = int(expire.timestamp())
    jti = str(uuid.uuid4())

    payload: dict[str, Any] = {
        "sub": subject,
        "iat": now,
        "exp": expire,
        "jti": jti,
        "type": "access",
    }

    if org_id is not None:
        payload["org_id"] = str(org_id)
    if org_role is not None:
        payload["org_role"] = org_role

    if extra_claims:
        payload.update(extra_claims)

    token = jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return token, jti, exp_unix


def decode_access_token(token: str) -> dict[str, Any] | None:
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
        if payload.get("type") != "access":
            return None
        return payload
    except JWTError:
        return None


def create_refresh_token(
    subject: str,
    expires_delta: timedelta | None = None,
    org_id: "uuid.UUID | None" = None,
    org_role: str | None = None,
) -> tuple[str, str, int]:
    """Return (encoded_token, sha256_hash, exp_unix_timestamp).

    org_id/org_role are carried in the refresh token so /token-refresh can
    re-issue an access token with the same org context.
    """
    if expires_delta is None:
        expires_delta = timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)

    now = datetime.now(timezone.utc)
    expire = now + expires_delta
    exp_unix = int(expire.timestamp())

    payload: dict[str, Any] = {
        "sub": subject,
        "iat": now,
        "exp": expire,
        "jti": str(uuid.uuid4()),
        "type": "refresh",
    }

    if org_id is not None:
        payload["org_id"] = str(org_id)
    if org_role is not None:
        payload["org_role"] = org_role

    token = jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    token_hash = hash_token(token)
    return token, token_hash, exp_unix


def decode_refresh_token(token: str) -> dict[str, Any] | None:
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
        if payload.get("type") != "refresh":
            return None
        return payload
    except JWTError:
        return None
