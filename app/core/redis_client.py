from redis.asyncio import ConnectionPool, Redis

from app.core.config import get_settings

_redis_pool: ConnectionPool | None = None
_redis_client: Redis | None = None


async def get_redis() -> Redis:
    global _redis_client, _redis_pool

    if _redis_client is None:
        settings = get_settings()
        _redis_pool = ConnectionPool.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            max_connections=20,
            protocol=2,  # Redis 5.x doesn't support RESP3 (HELLO 3); force RESP2
        )
        _redis_client = Redis(connection_pool=_redis_pool)

    return _redis_client


async def close_redis() -> None:
    global _redis_client, _redis_pool

    if _redis_client:
        await _redis_client.aclose()
        _redis_client = None

    if _redis_pool:
        await _redis_pool.disconnect()
        _redis_pool = None
