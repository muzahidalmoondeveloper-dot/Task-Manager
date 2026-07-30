from redis.asyncio import Redis

from app.core.redis_client import get_redis

_REFRESH_TOKEN_TTL = 30 * 24 * 3600  # 30 days, matches refresh token lifetime


class TokenCache:
    def __init__(self, redis: Redis):
        self.redis = redis

    async def blacklist_access_token(self, jti: str, ttl_seconds: int) -> None:
        """Store the JTI in Redis until the token naturally expires."""
        key = f"blacklist:access:{jti}"
        await self.redis.setex(key, ttl_seconds, "1")

    async def is_access_token_blacklisted(self, jti: str) -> bool:
        key = f"blacklist:access:{jti}"
        try:
            return (await self.redis.exists(key)) > 0
        except Exception:
            return False

    async def revoke_all_user_tokens(self, user_id: str) -> None:
        """Set a flag that causes all issued tokens for this user to be rejected."""
        key = f"user_tokens_revoked:{user_id}"
        await self.redis.setex(key, _REFRESH_TOKEN_TTL, "1")

    async def clear_user_access_token_blacklist(self, user_id: str) -> None:
        key = f"blacklist:user_access:{user_id}"
        await self.redis.delete(key)


_token_cache: TokenCache | None = None


async def get_token_cache() -> TokenCache:
    global _token_cache
    if _token_cache is None:
        redis = await get_redis()
        _token_cache = TokenCache(redis)
    return _token_cache
