"""Audit trail (spec Section 49, bounded slice) — one row per copilot tool
call. Never raises — a logging failure must never break the chat reply."""

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.copilot import AIToolExecution

logger = logging.getLogger("copilot.audit")


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
        ))
        await db.commit()
    except Exception:
        logger.exception("Failed to write AI tool execution audit row for tool=%s", tool_name)
