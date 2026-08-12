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


async def build_change_set_for_entities(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    session_id: int,
    user_id: int,
    tool_name: str,
    params: dict,
    affected: list[tuple[str, int, "datetime | None"]],
    affected_summary: str,
) -> AIChangeSet:
    """Entity-agnostic sibling of build_change_set() (architecture item 2 —
    Projects/Teams lifecycle) — for CONFIRM-tier tools whose affected
    records aren't Tasks (e.g. reassign_team_manager). `affected` is
    (entity_type, id, updated_at) tuples; captured_versions_json keys are
    "<entity_type>:<id>" so check_versions_fresh() can dispatch to the
    right model per key instead of assuming Task, while the plain-int keys
    build_change_set() already writes stay valid and unambiguous (an int
    string never contains ":")."""
    captured_versions = {
        f"{etype}:{eid}": (updated_at.isoformat() if updated_at else "")
        for etype, eid, updated_at in affected
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


async def get_change_set_for_update(db: AsyncSession, org_id: uuid.UUID, change_set_id: int) -> AIChangeSet | None:
    """Like get_change_set(), but with `SELECT ... FOR UPDATE` (security gap
    #2 from the strict acceptance audit — "change-set TOCTOU race"). Without
    this, two concurrent confirm requests for the same change set could both
    read status="pending" before either one committed its own status update,
    and both would proceed to apply the change — double-executing a
    reassignment/bulk-update/delete. Postgres blocks the second transaction's
    row lock until the first commits, and (under the default READ COMMITTED
    isolation this app runs at) re-reads the now-committed row once
    unblocked — so the second caller correctly observes status="executed"
    and is rejected by the same `if change_set.status != "pending"` check
    that already existed, instead of racing past it.

    Used ONLY by the two call sites that actually execute a change set
    (POST /change-sets/{id}/confirm and the admin-approval execute path) —
    every other read of a change set (status display, cancel) has nothing
    to protect against a concurrent double-apply and doesn't need to hold a
    row lock."""
    result = await db.execute(
        select(AIChangeSet)
        .where(
            AIChangeSet.id == change_set_id,
            AIChangeSet.organization_id == org_id,
        )
        .with_for_update()
    )
    return result.scalar_one_or_none()


def is_expired(change_set: AIChangeSet) -> bool:
    expires_at = change_set.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at < datetime.now(timezone.utc)


async def check_versions_fresh(db: AsyncSession, change_set: AIChangeSet) -> bool:
    """Optimistic-lock check — has any affected record changed since
    preview? Keys written by build_change_set() are plain task-id strings
    (legacy, Task-only); keys written by build_change_set_for_entities()
    are "<entity_type>:<id>" and dispatch to the matching model here."""
    from app.models.project import Project
    from app.models.team import Team

    entity_models = {"task": Task, "team": Team, "project": Project}

    for key, captured_iso in change_set.captured_versions_json.items():
        if ":" in key:
            entity_type, id_str = key.split(":", 1)
            model = entity_models.get(entity_type)
            if model is None:
                continue  # unknown entity type — nothing to compare against, don't block on it
            result = await db.execute(select(model.updated_at).where(model.id == int(id_str)))
        else:
            result = await db.execute(select(Task.updated_at).where(Task.id == int(key)))
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
