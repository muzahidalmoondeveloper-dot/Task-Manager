"""Layered Memory (spec Section 32.4, bounded) — stores only stable,
user-confirmed preferences (e.g. "prefers concise answers", "always wants
PDF reports") in `AISavedMemory`, never current operational facts (task
counts, assignments, statuses) — those always come from a live DB query, by
design, so this layer can never go stale in a way that causes a wrong
answer about system state. A tiny LLM classifier decides whether a message
contains anything worth remembering; most messages don't, so nothing is
written for them."""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.models.copilot import AISavedMemory
from app.services.copilot.intent_schemas import MemoryExtraction

logger = logging.getLogger("copilot.memory")

_MEMORY_EXTRACT_SYSTEM = """Decide if the user's message states a stable
personal preference worth remembering for future conversations (e.g.
preferred report format, communication style, timezone, recurring
priorities). Do NOT record one-off facts, current task/project state, or
anything that will change soon — that always comes from a live lookup, never
from memory.

Return ONLY JSON: {"has_preference": true/false, "key": "short_snake_case_key", "value": "the preference"}
If has_preference is false, key and value can be empty strings."""


async def get_saved_memories(db: AsyncSession, user_id: int) -> list[tuple[str, str]]:
    result = await db.execute(
        select(AISavedMemory.key, AISavedMemory.value).where(AISavedMemory.user_id == user_id)
    )
    return [(row.key, row.value) for row in result.all()]


async def maybe_learn_preference(llm, *, org_id, user_id: int, message: str) -> None:
    """Best-effort, fire-and-forget from the chat request — opens its own DB
    session (never reuses the request-scoped one), exactly like the
    existing topics.update_topic / background_email.py helpers, since it may
    still be running after the HTTP response has been sent."""
    try:
        # generate_structured() — security gap #3 fix, see planner.py's
        # matching comment.
        extraction = await llm.generate_structured(
            system_prompt=_MEMORY_EXTRACT_SYSTEM,
            user_prompt=message,
            schema=MemoryExtraction,
            temperature=0.0,
            capability="memory_extraction",
        )
        if not extraction.has_preference:
            return
        key = extraction.key.strip()[:100]
        value = extraction.value.strip()
        if not key or not value:
            return

        async with AsyncSessionLocal() as db:
            existing = await db.execute(
                select(AISavedMemory).where(AISavedMemory.user_id == user_id, AISavedMemory.key == key)
            )
            row = existing.scalar_one_or_none()
            if row is not None:
                row.value = value
                row.source_message = message[:2000]
            else:
                db.add(AISavedMemory(
                    organization_id=org_id, user_id=user_id, key=key, value=value,
                    source_message=message[:2000], confidence=0.8,
                ))
            await db.commit()
    except Exception:
        logger.info("Memory extraction skipped (non-critical)", exc_info=True)
