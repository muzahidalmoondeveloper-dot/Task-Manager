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

    # _redis_client/_redis_pool are cached globally, not per event loop. If a
    # previous caller ran under an asyncio.run() whose loop has since closed
    # (e.g. a prior test's own event loop) without itself calling
    # close_redis(), the cached client/pool are still holding connections
    # bound to that dead loop — attempting to gracefully close them raises
    # "Event loop is closed" even though our actual goal here (discard the
    # stale singleton so get_redis() builds a fresh one on this loop) still
    # succeeds. Swallow that specific teardown failure rather than letting it
    # mask the caller's real work.
    if _redis_client:
        try:
            await _redis_client.aclose()
        except RuntimeError:
            pass
        _redis_client = None

    if _redis_pool:
        try:
            await _redis_pool.disconnect()
        except RuntimeError:
            pass
        _redis_pool = None
