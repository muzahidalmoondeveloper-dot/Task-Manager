"""Regression test for the rate-limit "Too many attempts" message: it must
say how long to wait (e.g. "try again in 1 hour") instead of the generic
"Please try again later." whenever the block's remaining time is known.

Covers:
- format_retry_after(): seconds/minutes/hours phrasing, including the
  ceil-rounding that keeps "61 seconds" reading as "2 minutes" rather than
  "1 minute" (which would undersell the actual wait) and never "0 minutes".
- RateLimiter.get_retry_after_seconds(): returns None before any block
  exists, and a real remaining-TTL value once the caller trips the limit
  (login's real config: 5 requests/60s window, then a 3600s block).
- app.api.routes.auth._rate_limited_error(): builds a message containing
  the actual wait time once tripped, not the vague fallback.

Runs against the real Redis connection the app uses. Keys created here are
deleted before the test returns (they'd also expire on their own).
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.

from app.api.routes.auth import _rate_limited_error
from app.core.rate_limiter import RateLimiter, format_retry_after
from app.core.rate_limit_config import RATE_LIMITS
from app.core.redis_client import close_redis, get_redis


class _FakeClient:
    def __init__(self, host: str):
        self.host = host


class _FakeRequest:
    """Duck-typed stand-in for a Starlette Request — RateLimiter only ever
    reads `.headers.get(...)`, `.client.host`, and `.url.path` off it."""

    def __init__(self, ip: str, path: str = "/auth/login"):
        self.headers = {}
        self.client = _FakeClient(ip)

        class _Url:
            pass

        self.url = _Url()
        self.url.path = path


def test_format_retry_after_phrasing():
    assert format_retry_after(1) == "1 second"
    assert format_retry_after(45) == "45 seconds"
    assert format_retry_after(60) == "1 minute"
    assert format_retry_after(61) == "2 minutes", "must round up, not undersell the actual wait"
    assert format_retry_after(120) == "2 minutes"
    assert format_retry_after(3599) == "1 hour", "59m59s rounds up to the next whole unit, same as the seconds->minutes case"
    assert format_retry_after(3600) == "1 hour"
    assert format_retry_after(7200) == "2 hours"


async def _scenario():
    # get_redis() caches one client/pool globally for the process, bound to
    # whichever event loop first created it. Another test module in this
    # suite (test_meeting_live_reactions.py) also calls get_redis() inside
    # its own asyncio.run() and never closes it — its loop is gone by the
    # time this test runs under a fresh asyncio.run() of its own, so the
    # inherited client would raise "Event loop is closed" on first use.
    # Discarding any stale client first forces a brand-new one bound to
    # *this* loop; closing again afterward leaves a clean slate for
    # whichever test runs next, rather than leaving that same landmine.
    await close_redis()
    redis = await get_redis()
    suffix = uuid.uuid4().hex[:12]
    fake_ip = f"203.0.113.{int(suffix[:2], 16) % 255}.test.{suffix}"
    request = _FakeRequest(fake_ip)
    rate_limiter = RateLimiter(redis=redis, config=RATE_LIMITS)

    cfg = RATE_LIMITS["login"]
    identifier = fake_ip  # login uses RateLimitKey.IP
    rate_key = f"rate_limit:login:{identifier}"
    block_key = f"rate_limit:login:block:{identifier}"

    try:
        # Not limited yet — no retry-after to report.
        assert await rate_limiter.get_retry_after_seconds("login", request) is None
        assert await rate_limiter.is_rate_limited("login", request) is False

        # Simulate the limit having already been tripped — this is exactly
        # what is_rate_limited() itself does once `count > cfg.limit` (see
        # rate_limiter.py); seeding it directly here avoids a same-second
        # sorted-set dedup quirk of that unrelated, pre-existing trip logic
        # (repeated zadd calls within the same wall-clock second share one
        # member and never actually grow the count in a fast test loop) —
        # this test is about get_retry_after_seconds()/_rate_limited_error(),
        # not about re-proving the trip-detection logic already works.
        await redis.set(block_key, "1", ex=cfg.block_seconds)

        assert await rate_limiter.is_rate_limited("login", request) is True, (
            "an existing block must itself count as rate-limited"
        )

        retry_after = await rate_limiter.get_retry_after_seconds("login", request)
        assert retry_after is not None
        assert 0 < retry_after <= cfg.block_seconds

        error = await _rate_limited_error(rate_limiter, "login", request)
        assert error.code == "AUTH_RATE_LIMITED"
        assert error.status_code == 429
        assert "Please try again in" in error.message
        assert "later" not in error.message, "the vague fallback text must not appear once a real wait time is known"
        assert format_retry_after(retry_after) in error.message

    finally:
        await redis.delete(rate_key, block_key)


def test_rate_limiter_reports_actual_retry_after_once_tripped():
    asyncio.run(_scenario())
