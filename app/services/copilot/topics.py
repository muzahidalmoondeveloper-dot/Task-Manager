"""Topic System (spec Section 9, lightweight) — one flat, rolling-summary
row per chat session's current subject. This supplements the existing
last-20-messages history text passed to the LLM; it never replaces it, and
a failure here must never break the chat reply (same "best effort" contract
as the existing _auto_title_session)."""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.models.copilot import AITopic

logger = logging.getLogger("copilot.topics")

_TOPIC_UPDATE_SYSTEM = """You maintain a rolling summary of what a chat session is currently about.

Given the previous topic state and the latest exchange, decide:
- "continue": the conversation is still about the same subject — update the summary/title/open_items to reflect the latest exchange.
- "new_topic": the user has clearly moved on to an unrelated subject — start fresh.

Return ONLY JSON:
{
  "action": "continue" or "new_topic",
  "title": "short (max 6 words) title for the current topic",
  "summary": "1-3 sentence rolling summary of what's been discussed and decided",
  "open_items": ["short phrase", "..."]
}"""


async def get_active_topic(db: AsyncSession, session_id: int) -> AITopic | None:
    result = await db.execute(
        select(AITopic)
        .where(AITopic.session_id == session_id, AITopic.status == "active")
        .order_by(AITopic.last_active_at.desc())
    )
    return result.scalars().first()


async def update_topic(llm, session_id: int, message: str, reply: str) -> None:
    """Best-effort, fire-and-forget from the chat request — opens its own DB
    session (never reuses the request-scoped one) since it may still be
    running after the HTTP response has been sent and the request's session
    closed, exactly like the existing background_email.py helpers do."""
    try:
        async with AsyncSessionLocal() as db:
            existing = await get_active_topic(db, session_id)
            prior_state = (
                f"Current topic: {existing.title}\nSummary so far: {existing.summary}\nOpen items: {existing.open_items}"
                if existing else "No topic yet — this is the first exchange."
            )
            result = await llm.generate_text(
                system_prompt=_TOPIC_UPDATE_SYSTEM,
                user_prompt=f"{prior_state}\n\nLatest user message: {message}\nLatest assistant reply: {reply[:500]}",
                temperature=0.1,
                response_format="json",
            )
            import json
            data = json.loads(result.text.strip().strip("`").removeprefix("json").strip())

            if data.get("action") == "new_topic" and existing is not None:
                existing.status = "completed"
                await db.flush()
                existing = None

            if existing is None:
                existing = AITopic(session_id=session_id, title=data.get("title", "General")[:255])
                db.add(existing)

            existing.title = data.get("title", existing.title)[:255]
            existing.summary = data.get("summary", existing.summary)
            existing.open_items = data.get("open_items", existing.open_items) or []
            await db.commit()
            logger.info(
                "Topic updated: session=%s action=%s title=%r summary=%r open_items=%s",
                session_id, data.get("action"), existing.title, existing.summary, existing.open_items,
            )
    except Exception:
        logger.info("Topic update skipped (non-critical) for session=%s", session_id, exc_info=True)
