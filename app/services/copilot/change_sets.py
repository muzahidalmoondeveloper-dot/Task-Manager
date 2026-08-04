"""Change-Set System (spec Section 24) — a frozen preview of a pending write
that must be explicitly confirmed before anything touches the database."""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import ChatSession
from app.models.copilot import AIChangeSet
from app.models.task import Task

CHANGE_SET_TTL_MINUTES = 10


async def build_change_set(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    session_id: int,
    user_id: int,
    tool_name: str,
    params: dict,
    affected_tasks: list[Task],
    affected_summary: str,
) -> AIChangeSet:
    captured_versions = {
        str(t.id): (t.updated_at.isoformat() if t.updated_at else "")
        for t in affected_tasks
    }
    change_set = AIChangeSet(
        organization_id=org_id,
        session_id=session_id,
        created_by_id=user_id,
        tool_name=tool_name,
        params_json=params,
        captured_versions_json=captured_versions,
        affected_summary=affected_summary,
        status="pending",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=CHANGE_SET_TTL_MINUTES),
    )
    db.add(change_set)
    await db.flush()
    # Conversation state machine (spec Section 8) — the session now blocks
    # on this specific change set until confirmed/cancelled/expired.
    await db.execute(
        update(ChatSession)
        .where(ChatSession.id == session_id)
        .values(state="awaiting_confirmation", pending_change_set_id=change_set.id)
    )
    return change_set


async def clear_session_state(db: AsyncSession, session_id: int) -> None:
    await db.execute(
        update(ChatSession)
        .where(ChatSession.id == session_id, ChatSession.pending_change_set_id.is_not(None))
        .values(state="idle", pending_change_set_id=None)
    )


async def get_change_set(db: AsyncSession, org_id: uuid.UUID, change_set_id: int) -> AIChangeSet | None:
    result = await db.execute(
        select(AIChangeSet).where(
            AIChangeSet.id == change_set_id,
            AIChangeSet.organization_id == org_id,
        )
    )
    return result.scalar_one_or_none()


def is_expired(change_set: AIChangeSet) -> bool:
    expires_at = change_set.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at < datetime.now(timezone.utc)


async def check_versions_fresh(db: AsyncSession, change_set: AIChangeSet) -> bool:
    """Optimistic-lock check — has any affected task changed since preview?"""
    for task_id_str, captured_iso in change_set.captured_versions_json.items():
        result = await db.execute(select(Task.updated_at).where(Task.id == int(task_id_str)))
        current = result.scalar_one_or_none()
        current_iso = current.isoformat() if current else ""
        if current_iso != captured_iso:
            return False
    return True


async def cancel_change_set(db: AsyncSession, change_set: AIChangeSet) -> None:
    change_set.status = "cancelled"
    await clear_session_state(db, change_set.session_id)
    await db.commit()


async def expire_change_set(db: AsyncSession, change_set: AIChangeSet) -> None:
    change_set.status = "expired"
    await clear_session_state(db, change_set.session_id)
    await db.commit()
