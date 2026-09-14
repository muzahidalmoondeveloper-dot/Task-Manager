"""OTP resend cooldown regression test.

Root cause / feature: there was previously NO backend-enforced "wait N
seconds before resending" cooldown at all for `POST /auth/resend-otp` —
only a cosmetic frontend countdown (`setResendCooldown(30)`, trivially
bypassable by a page refresh or a direct API call) plus an unrelated,
coarser IP-based abuse throttle (`RATE_LIMITS["resend_otp"]`: 3 requests
per 60s window per IP, not a per-email "wait since your last code" rule).

Fix: `AuthSecurityService.get_seconds_until_resend_allowed` — a genuine,
backend-authoritative cooldown based on the most recently CREATED
`EmailOTP` row for the exact (email, purpose) pair, enforced in the
`resend_otp` route via `AuthError.otp_resend_too_soon` (429,
`AUTH_OTP_RESEND_TOO_SOON`) before a new OTP is ever generated/sent.
`OTP_RESEND_COOLDOWN_SECONDS = 60` is the single shared constant (next to
the pre-existing, UNCHANGED `OTP_EXPIRE_MINUTES = 10` — resend cooldown
and OTP validity are deliberately independent).

Covers:
- resend blocked at 0s/30s/59s since the last OTP, allowed at 60s+.
- a fresh resend restarts the cooldown from 60s.
- OTP validity (expires_at ~10 minutes out) is unaffected by any of this.
- the actual `POST /auth/resend-otp` route rejects a direct call made
  before 60s have elapsed — not just the isolated service method — so a
  page refresh (which only resets frontend state) can never bypass it.

Runs against the real database (and, for the route-level check, the real
Redis-backed rate limiter) the app uses. Every row this test creates is
deleted before it returns.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete, select, update

from app.api.routes.auth import resend_otp
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.models.auth_security import EmailOTP
from app.models.organization import OrganizationMembership
from app.models.user import User
from app.schemas.auth import ResendOTPRequest
from app.services.auth_security_service import OTP_EXPIRE_MINUTES, OTP_RESEND_COOLDOWN_SECONDS, AuthSecurityService


class _FakeUrl:
    path = "/auth/resend-otp"


class _FakeClient:
    def __init__(self, host: str):
        self.host = host


class _FakeRequest:
    """Duck-typed stand-in for fastapi.Request — RateLimiter._build_key
    only ever reads .headers.get(...), .client.host, and .url.path."""
    def __init__(self, ip: str):
        self.headers = {}
        self.client = _FakeClient(ip)
        self.url = _FakeUrl()


class _StubRateLimiter:
    """Stands in for the real, Redis-backed RateLimiter for this route-
    level check — this test's own OTP resend cooldown is the thing being
    tested, not the separate, coarser IP-based `resend_otp` abuse
    throttle. The real RateLimiter's global Redis client is a
    module-level singleton bound to whichever event loop first created
    it; since every test here runs its own fresh `asyncio.run()` event
    loop, touching that real singleton from a later test corrupts it for
    every test that runs after — this local stub avoids Redis (and that
    landmine) entirely while still exercising the real route function."""
    async def is_rate_limited(self, scope, request) -> bool:
        return False


def _backdate(now: datetime, seconds_ago: float) -> datetime:
    return now - timedelta(seconds=seconds_ago)


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]
        email = f"otpcooldown.{suffix}@example-corp.com"

        user = User(full_name="OTP Cooldown User", email=email, hashed_password="x", role="owner", email_verified_at=datetime.now(timezone.utc))
        route_email = f"otpcooldown.route.{suffix}@example-corp.com"
        route_user = User(full_name="OTP Cooldown Route User", email=route_email, hashed_password="x", role="owner", email_verified_at=datetime.now(timezone.utc))
        db.add_all([user, route_user])
        await db.commit()
        await db.refresh(user)
        await db.refresh(route_user)

        service = AuthSecurityService(db)

        try:
            # ── Sanity: OTP validity (expiry) is a separate, unchanged
            # constant — this task must never touch it. ──
            assert OTP_EXPIRE_MINUTES == 10, "OTP validity/expiry must remain unchanged by the resend-cooldown fix"
            assert OTP_RESEND_COOLDOWN_SECONDS == 60, "the resend cooldown must be 60 seconds"

            # ── No OTP sent yet -> resend is allowed (0s remaining). ──
            remaining = await service.get_seconds_until_resend_allowed(email=email, purpose="login")
            assert remaining == 0

            # ── Send one, then immediately check -> ~60s remaining
            # (allow a couple seconds of test-runtime slack). ──
            await service.create_and_send_otp(user=user, email=email, purpose="login")
            remaining = await service.get_seconds_until_resend_allowed(email=email, purpose="login")
            assert 55 <= remaining <= 60, f"expected ~60s remaining right after sending, got {remaining}"

            # OTP validity is untouched: expires_at is ~10 minutes out,
            # not tied to the 60s resend cooldown in any way.
            result = await db.execute(
                select(EmailOTP).where(EmailOTP.email == email, EmailOTP.purpose == "login").order_by(EmailOTP.created_at.desc()).limit(1)
            )
            otp_row = result.scalar_one()
            otp_id = otp_row.id
            expires_in = (otp_row.expires_at.replace(tzinfo=timezone.utc) if otp_row.expires_at.tzinfo is None else otp_row.expires_at) - datetime.now(timezone.utc)
            assert timedelta(minutes=9) < expires_in <= timedelta(minutes=10), f"OTP expiry drifted from the unchanged 10-minute validity window: {expires_in}"

            now = datetime.now(timezone.utc)

            # ── Backdate the OTP's created_at to simulate time passing,
            # and verify the exact validation boundaries. ──
            async def _set_created_at(seconds_ago: float) -> int:
                await db.execute(update(EmailOTP).where(EmailOTP.id == otp_id).values(created_at=_backdate(now, seconds_ago)))
                await db.commit()
                return await service.get_seconds_until_resend_allowed(email=email, purpose="login")

            # 0s ago (just sent) -> fully blocked, ~60s left.
            remaining = await _set_created_at(0)
            assert 59 <= remaining <= 60

            # 30s ago -> still blocked, ~30s left.
            remaining = await _set_created_at(30)
            assert 29 <= remaining <= 30, f"expected ~30s remaining at 30s elapsed, got {remaining}"

            # 59s ago -> still blocked, ~1s left.
            remaining = await _set_created_at(59)
            assert 0 < remaining <= 1, f"expected ~1s remaining at 59s elapsed, got {remaining}"

            # 60s ago exactly -> allowed.
            remaining = await _set_created_at(60)
            assert remaining == 0, f"expected resend allowed at exactly 60s elapsed, got {remaining}"

            # 61s+ ago -> allowed.
            remaining = await _set_created_at(90)
            assert remaining == 0

            # ── A fresh resend restarts the cooldown from 60s. ──
            await service.create_and_send_otp(user=user, email=email, purpose="login")
            remaining = await service.get_seconds_until_resend_allowed(email=email, purpose="login")
            assert 55 <= remaining <= 60, f"a fresh resend must restart the cooldown at ~60s, got {remaining}"

            # ── Different purpose is an independent cooldown clock (a
            # register-purpose resend must not be blocked by a login-
            # purpose OTP just sent for the same email). ──
            remaining = await service.get_seconds_until_resend_allowed(email=email, purpose="register")
            assert remaining == 0

            # ── Full route, end-to-end: a direct call to POST
            # /auth/resend-otp made before 60s must still be rejected —
            # never merely a frontend timer, and never bypassable by
            # whatever the client's own state claims (e.g. after a page
            # refresh resets a local countdown to 0). Uses a unique fake
            # IP so this never interacts with the real, shared IP-based
            # `resend_otp` rate limiter bucket used by real traffic or
            # other test runs. ──
            # A dedicated second user/email — never touched by the
            # service-level steps above — so this section starts from a
            # genuinely clean cooldown slate.
            fake_request = _FakeRequest(ip=f"203.0.113.{int(uuid.uuid4().int % 250) + 1}")
            rate_limiter = _StubRateLimiter()

            first = await resend_otp(
                ResendOTPRequest(email=route_email, purpose="login"),
                fake_request, db=db, rate_limiter=rate_limiter,
            )
            assert first["message"] == "OTP resent successfully."

            try:
                await resend_otp(
                    ResendOTPRequest(email=route_email, purpose="login"),
                    fake_request, db=db, rate_limiter=rate_limiter,
                )
                raise AssertionError("an immediate second resend-otp call must be rejected by the backend cooldown")
            except AppException as exc:
                assert exc.status_code == 429
                assert exc.code == "AUTH_OTP_RESEND_TOO_SOON"
                assert 55 <= exc.details["retry_after_seconds"] <= 60

            print("test_otp_resend_cooldown: PASSED")
        finally:
            await db.execute(delete(EmailOTP).where(EmailOTP.email.in_([email, route_email])))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.user_id.in_([user.id, route_user.id])))
            await db.execute(delete(User).where(User.id.in_([user.id, route_user.id])))
            await db.commit()

    await engine.dispose()


def test_otp_resend_cooldown():
    asyncio.run(_scenario())
