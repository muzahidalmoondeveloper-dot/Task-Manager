import time

from fastapi import Request

from app.core.rate_limit_config import RATE_LIMITS, RateLimitConfig, RateLimitKey
from app.core.redis_client import get_redis


def format_retry_after(seconds: int) -> str:
    """Human-friendly duration for a "try again in ..." message — the
    smallest unit that keeps the number readable (whole minutes round up
    so "61 seconds" reads as "2 minutes", never "0 minutes")."""
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    minutes = -(-seconds // 60)  # ceil division
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours = -(-minutes // 60)
    return f"{hours} hour{'s' if hours != 1 else ''}"


class RateLimiter:
    def __init__(self, redis, config: dict[str, RateLimitConfig], prefix: str = "rate_limit"):
        self.redis = redis
        self.config = config
        self.prefix = prefix

    async def is_rate_limited(self, scope: str, request: Request) -> bool:
        if scope not in self.config:
            raise RuntimeError(f"Rate limit scope '{scope}' not configured")

        cfg = self.config[scope]
        identifier = self._build_key(request, cfg.key_type)

        rate_key = f"{self.prefix}:{scope}:{identifier}"
        block_key = f"{self.prefix}:{scope}:block:{identifier}"

        if await self.redis.exists(block_key):
            return True

        now = int(time.time())
        pipe = self.redis.pipeline()
        await pipe.zadd(rate_key, {now: now})
        await pipe.zremrangebyscore(rate_key, 0, now - cfg.window_seconds)
        await pipe.zcard(rate_key)
        await pipe.expire(rate_key, cfg.window_seconds)
        _, _, count, _ = await pipe.execute()

        if count > cfg.limit:
            if cfg.block_seconds:
                await self.redis.set(block_key, "1", ex=cfg.block_seconds)
            return True

        return False

    async def get_retry_after_seconds(self, scope: str, request: Request) -> int | None:
        """Seconds remaining until this caller's block on `scope` clears —
        call this right after is_rate_limited() returns True, so the
        rejection message can say "try again in X" instead of a vague
        "later". Returns None if there's no active block to report (the
        window will simply reset on its own in that case)."""
        if scope not in self.config:
            raise RuntimeError(f"Rate limit scope '{scope}' not configured")
        cfg = self.config[scope]
        identifier = self._build_key(request, cfg.key_type)
        block_key = f"{self.prefix}:{scope}:block:{identifier}"
        ttl = await self.redis.ttl(block_key)
        return ttl if ttl and ttl > 0 else None

    def _build_key(self, request: Request, key_type: RateLimitKey) -> str:
        forwarded = request.headers.get("x-forwarded-for")
        ip = (
            forwarded.split(",")[0].strip()
            if forwarded
            else (request.client.host if request.client else "unknown")
        )
        if key_type == RateLimitKey.IP_ENDPOINT:
            return f"{ip}:{request.url.path}"
        return ip


_rate_limiter: RateLimiter | None = None


async def get_rate_limiter() -> RateLimiter:
    global _rate_limiter
    if _rate_limiter is None:
        redis = await get_redis()
        _rate_limiter = RateLimiter(redis=redis, config=RATE_LIMITS)
    return _rate_limiter
