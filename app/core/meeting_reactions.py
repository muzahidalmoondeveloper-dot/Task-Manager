"""Ephemeral live-meeting reactions (👍 👏 ❤️ 😊) — Redis-backed, never
written to Postgres. These exist purely to be picked up by the next poll
from anyone else currently viewing the same meeting, then expire on their
own; there is no reaction history feature to preserve them for.

Reuses the app's existing Redis connection (app.core.redis_client) rather
than introducing a new real-time transport (WebSocket/SSE) — this app has
none today, and the live meeting panel already re-polls GET /meetings/{id}
every 2s, so a lightweight "what's new since reaction id N" endpoint on the
same cadence is the natural fit rather than a parallel push mechanism.
"""

import json
import time

from redis.asyncio import Redis

_TTL_SECONDS = 30  # a reaction is only ever relevant for one poll cycle or two


def _list_key(meeting_id: int) -> str:
    return f"meeting:{meeting_id}:reactions"


def _seq_key(meeting_id: int) -> str:
    return f"meeting:{meeting_id}:reactions:seq"


async def push_reaction(redis: Redis, meeting_id: int, emoji: str, user_id: int) -> dict:
    """Append a reaction and return it (with its assigned id) for the
    sender's own immediate response — everyone else picks it up via the
    next list_reactions_since() poll."""
    seq_key = _seq_key(meeting_id)
    reaction_id = await redis.incr(seq_key)
    await redis.expire(seq_key, _TTL_SECONDS)

    entry = {"id": reaction_id, "emoji": emoji, "user_id": user_id, "ts": time.time()}
    key = _list_key(meeting_id)
    await redis.rpush(key, json.dumps(entry))
    await redis.ltrim(key, -200, -1)  # bounded — this is a live feed, not a log
    await redis.expire(key, _TTL_SECONDS)
    return entry


async def list_reactions_since(redis: Redis, meeting_id: int, since_id: int) -> list[dict]:
    raw = await redis.lrange(_list_key(meeting_id), 0, -1)
    entries = [json.loads(r) for r in raw]
    return [e for e in entries if e["id"] > since_id]
