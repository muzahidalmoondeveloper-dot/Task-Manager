"""Multi-Level Approvals (spec Section 47, bounded) — for the one concrete
scenario this app currently gates on it (see risk.requires_admin_approval):
a team_manager's bulk task update needs an admin's sign-off, rather than
being self-confirmable like every other change set."""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import ChatSession
from app.models.copilot import AIApprovalRequest, AIChangeSet

APPROVAL_TTL_MINUTES = 60 * 24  # a day in an admin's inbox before it expires


async def create_approval_request(
    db: AsyncSession, *, org_id: uuid.UUID, session_id: int, change_set: AIChangeSet,
    requested_by_id: int, approver_role: str, reason: str,
) -> AIApprovalRequest:
    approval = AIApprovalRequest(
        organization_id=org_id,
        session_id=session_id,
        change_set_id=change_set.id,
        requested_by_id=requested_by_id,
        approver_role=approver_role,
        reason=reason,
        status="pending",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=APPROVAL_TTL_MINUTES),
    )
    db.add(approval)
    await db.flush()

    state = "awaiting_admin_approval" if approver_role == "admin" else "awaiting_manager_approval"
    await db.execute(
        update(ChatSession).where(ChatSession.id == session_id)
        .values(state=state, pending_approval_id=approval.id)
    )
    return approval


async def get_approval(db: AsyncSession, org_id: uuid.UUID, approval_id: int) -> AIApprovalRequest | None:
    result = await db.execute(
        select(AIApprovalRequest).where(
            AIApprovalRequest.id == approval_id,
            AIApprovalRequest.organization_id == org_id,
        )
    )
    return result.scalar_one_or_none()


async def list_pending_for_approver(db: AsyncSession, org_id: uuid.UUID, approver_role: str) -> list[AIApprovalRequest]:
    result = await db.execute(
        select(AIApprovalRequest)
        .where(
            AIApprovalRequest.organization_id == org_id,
            AIApprovalRequest.approver_role == approver_role,
            AIApprovalRequest.status == "pending",
        )
        .order_by(AIApprovalRequest.created_at.desc())
    )
    return list(result.scalars().all())


def is_expired(approval: AIApprovalRequest) -> bool:
    expires_at = approval.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at < datetime.now(timezone.utc)


async def clear_session_state(db: AsyncSession, session_id: int) -> None:
    await db.execute(
        update(ChatSession)
        .where(ChatSession.id == session_id, ChatSession.pending_approval_id.is_not(None))
        .values(state="idle", pending_approval_id=None)
    )


async def reject_approval(db: AsyncSession, approval: AIApprovalRequest, change_set: AIChangeSet, decided_by_id: int) -> None:
    approval.status = "rejected"
    approval.decided_by_id = decided_by_id
    approval.decided_at = datetime.now(timezone.utc)
    change_set.status = "cancelled"
    await clear_session_state(db, approval.session_id)
    await db.commit()
