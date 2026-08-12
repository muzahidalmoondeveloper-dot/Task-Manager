"""Audit trail (spec Section 49, bounded slice) — one row per copilot tool
call. Never raises — a logging failure must never break the chat reply."""

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.copilot import AIToolExecution

logger = logging.getLogger("copilot.audit")

# Idempotency dedup window (architecture item 13) — a repeated call with the
# same key older than this is treated as a genuinely new request, not a
# retry of the earlier one (e.g. "create a task called Follow up" said again
# tomorrow should create a second task, not be silently swallowed forever).
IDEMPOTENCY_WINDOW = timedelta(minutes=5)


async def log_tool_execution(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    session_id: int | None,
    user_id: int,
    tool_name: str,
    risk_level: str,
    policy_decision: str,
    params: dict,
    result_summary: str,
    success: bool,
    trace_id: str | None = None,
    idempotency_key: str | None = None,
) -> None:
    try:
        db.add(AIToolExecution(
            organization_id=org_id,
            session_id=session_id,
            user_id=user_id,
            tool_name=tool_name,
            risk_level=risk_level,
            policy_decision=policy_decision,
            params_json=params,
            result_summary=result_summary[:2000],
            success=success,
            trace_id=trace_id,
            idempotency_key=idempotency_key,
        ))
        await db.commit()
    except Exception:
        logger.exception("Failed to write AI tool execution audit row for tool=%s", tool_name)


async def find_recent_success(
    db: AsyncSession, *, org_id: uuid.UUID, tool_name: str, idempotency_key: str,
) -> AIToolExecution | None:
    """Idempotency lookup (architecture item 13): a prior *successful*
    execution of this exact tool+params (the idempotency_key is a hash of
    both — see registry.py) within the dedup window means this call is a
    retry of one that already happened, not a new request. Only successful
    rows count — a prior failure must not block a genuine retry."""
    if not idempotency_key:
        return None
    cutoff = datetime.now(timezone.utc) - IDEMPOTENCY_WINDOW
    result = await db.execute(
        select(AIToolExecution)
        .where(
            AIToolExecution.organization_id == org_id,
            AIToolExecution.tool_name == tool_name,
            AIToolExecution.idempotency_key == idempotency_key,
            AIToolExecution.success.is_(True),
            AIToolExecution.created_at >= cutoff,
        )
        .order_by(AIToolExecution.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()
