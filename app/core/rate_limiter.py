import time

from fastapi import Request

from app.core.rate_limit_config import RATE_LIMITS, RateLimitConfig, RateLimitKey
from app.core.redis_client import get_redis


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
