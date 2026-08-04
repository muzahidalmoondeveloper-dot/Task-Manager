import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi import status as http_status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.tenant import TenantContext, enforce_feature, get_tenant_context
from app.repositories.chat_repository import ChatRepository
from app.schemas.chat import (
    ApprovalRequestRead,
    ChatAction,
    ChatMessageRequest,
    ChatMessageResponse,
    ChatSessionRead,
    ChatSessionWithMessages,
)
from app.services.chat_service import ChatService
from app.services.copilot import approvals, change_sets, undo
from app.services.copilot.transaction import execute_confirmed_change_set
from app.services.file_extractor import extract_text, supported

router = APIRouter(prefix="/chat", tags=["Chat"])
FILE_SIZE_LIMIT = 20 * 1024 * 1024


@router.post("/message", response_model=ChatMessageResponse)
async def send_message(
    payload: ChatMessageRequest,
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db),
):
    enforce_feature(tenant, "has_ai_features")
    if not payload.message or not payload.message.strip():
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail="Message cannot be empty.")
    service = ChatService(db, tenant.organization_id)
    return await service.handle_message(user=tenant.user, message=payload.message.strip(), session_id=payload.session_id, org_role=tenant.org_role)


@router.post("/upload", response_model=ChatMessageResponse)
async def upload_file_message(
    file: UploadFile = File(...),
    message: str = Form(""),
    session_id: int | None = Form(None),
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db),
):
    enforce_feature(tenant, "has_ai_features")
    if not supported(file.filename or ""):
        raise HTTPException(status_code=http_status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="Unsupported file type.")
    content = await file.read()
    if len(content) > FILE_SIZE_LIMIT:
        raise HTTPException(status_code=http_status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="File too large. Maximum size is 20 MB.")
    if not content:
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty.")
    try:
        file_text = await extract_text(file.filename, content)
    except ValueError as exc:
        raise HTTPException(status_code=http_status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Could not extract text: {exc}")
    service = ChatService(db, tenant.organization_id)
    return await service.handle_message(user=tenant.user, message=message.strip(), session_id=session_id, org_role=tenant.org_role, file_context={"filename": file.filename, "text": file_text, "size_bytes": len(content)})


@router.get("/sessions", response_model=list[ChatSessionRead])
async def list_sessions(tenant: TenantContext = Depends(get_tenant_context)):
    repo = ChatRepository(tenant.db, tenant.organization_id)
    return await repo.list_sessions_for_user(tenant.user.id)


@router.get("/sessions/{session_id}", response_model=ChatSessionWithMessages)
async def get_session(session_id: int, tenant: TenantContext = Depends(get_tenant_context)):
    repo = ChatRepository(tenant.db, tenant.organization_id)
    session = await repo.get_session(session_id)
    if session is None or session.user_id != tenant.user.id:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Session not found.")
    return ChatSessionWithMessages(id=session.id, title=session.title, created_at=session.created_at, updated_at=session.updated_at, messages=[{"id": m.id, "session_id": m.session_id, "role": m.role, "content": m.content, "created_at": m.created_at} for m in session.messages])


@router.delete("/sessions/{session_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: int,
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db),
):
    repo = ChatRepository(db, tenant.organization_id)
    session = await repo.get_session(session_id)
    if session is None or session.user_id != tenant.user.id:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Session not found.")
    await db.delete(session)
    await db.commit()
    return None


# ── Change-set confirmation (spec Section 24, 27) ────────────────────────────

async def _record_action_exchange(
    db: AsyncSession, tenant: TenantContext, session_id: int, user_label: str, reply: str, actions: list[ChatAction],
    require_owner: bool = True,
) -> ChatMessageResponse:
    repo = ChatRepository(db, tenant.organization_id)
    session = await repo.get_session(session_id)
    if session is None or (require_owner and session.user_id != tenant.user.id):
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Session not found.")
    user_msg = await repo.add_message(session_id, "user", user_label)
    assistant_msg = await repo.add_message(session_id, "assistant", reply)
    await repo.touch_session(session)
    return ChatMessageResponse(
        session_id=session_id,
        user_message=user_msg,
        assistant_message=assistant_msg,
        actions=actions,
    )


@router.post("/change-sets/{change_set_id}/confirm", response_model=ChatMessageResponse)
async def confirm_change_set(
    change_set_id: int,
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db),
):
    enforce_feature(tenant, "has_ai_features")
    change_set = await change_sets.get_change_set(db, tenant.organization_id, change_set_id)
    if change_set is None or change_set.created_by_id != tenant.user.id:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Change set not found.")
    if change_set.status != "pending":
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail=f"This action is already {change_set.status}.")
    if change_sets.is_expired(change_set):
        await change_sets.expire_change_set(db, change_set)
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail="This confirmation has expired — please ask again.")

    result = await execute_confirmed_change_set(
        db, org_id=tenant.organization_id, org_role=tenant.org_role, change_set=change_set,
        trace_id=str(uuid.uuid4()),
    )
    actions: list[ChatAction] = []
    if result.operation_id:
        actions.append(ChatAction(
            type="undo_available",
            label="Undo",
            payload={"operation_id": result.operation_id},
        ))
    return await _record_action_exchange(db, tenant, change_set.session_id, "[Confirmed]", result.message, actions)


@router.post("/change-sets/{change_set_id}/cancel", response_model=ChatMessageResponse)
async def cancel_change_set(
    change_set_id: int,
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db),
):
    enforce_feature(tenant, "has_ai_features")
    change_set = await change_sets.get_change_set(db, tenant.organization_id, change_set_id)
    if change_set is None or change_set.created_by_id != tenant.user.id:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Change set not found.")
    if change_set.status != "pending":
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail=f"This action is already {change_set.status}.")

    await change_sets.cancel_change_set(db, change_set)
    return await _record_action_exchange(
        db, tenant, change_set.session_id, "[Cancelled]", "Okay, I didn't make that change.", [],
    )


# ── Undo (spec Section 46) ───────────────────────────────────────────────────

@router.post("/operations/{operation_id}/undo", response_model=ChatMessageResponse)
async def undo_operation(
    operation_id: int,
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db),
):
    enforce_feature(tenant, "has_ai_features")
    operation = await undo.get_operation(db, tenant.organization_id, operation_id)
    if operation is None or operation.created_by_id != tenant.user.id:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Nothing to undo.")
    if operation.undone:
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail="This was already undone.")
    if undo.is_expired(operation):
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail="The undo window for this action has passed.")

    message = await undo.execute_undo(db, tenant.organization_id, operation)
    return await _record_action_exchange(db, tenant, operation.session_id, "[Undo]", message, [])


# ── Multi-level approvals (spec Section 47) ──────────────────────────────────

@router.get("/approvals", response_model=list[ApprovalRequestRead])
async def list_approvals(tenant: TenantContext = Depends(get_tenant_context)):
    enforce_feature(tenant, "has_ai_features")
    if not tenant.is_admin_or_owner:
        raise HTTPException(status_code=http_status.HTTP_403_FORBIDDEN, detail="Only admins can view pending approvals.")
    return await approvals.list_pending_for_approver(tenant.db, tenant.organization_id, "admin")


@router.post("/approvals/{approval_id}/approve", response_model=ChatMessageResponse)
async def approve_request(
    approval_id: int,
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db),
):
    enforce_feature(tenant, "has_ai_features")
    if not tenant.is_admin_or_owner:
        raise HTTPException(status_code=http_status.HTTP_403_FORBIDDEN, detail="Only admins can approve this.")
    approval = await approvals.get_approval(db, tenant.organization_id, approval_id)
    if approval is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Approval request not found.")
    if approval.status != "pending":
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail=f"This request is already {approval.status}.")
    if approvals.is_expired(approval):
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail="This approval request has expired.")

    change_set = await change_sets.get_change_set(db, tenant.organization_id, approval.change_set_id)
    if change_set is None or change_set.status != "pending":
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail="The underlying change is no longer pending.")

    result = await execute_confirmed_change_set(
        db, org_id=tenant.organization_id, org_role="admin", change_set=change_set,
        trace_id=str(uuid.uuid4()),
    )
    approval.status = "approved" if result.success else "rejected"
    approval.decided_by_id = tenant.user.id
    from datetime import datetime, timezone
    approval.decided_at = datetime.now(timezone.utc)
    await approvals.clear_session_state(db, approval.session_id)
    await db.commit()

    actions: list[ChatAction] = []
    if result.operation_id:
        actions.append(ChatAction(type="undo_available", label="Undo", payload={"operation_id": result.operation_id}))
    return await _record_action_exchange(
        db, tenant, approval.session_id, "[Approved by admin]", result.message, actions, require_owner=False,
    )


@router.post("/approvals/{approval_id}/reject", response_model=ChatMessageResponse)
async def reject_request(
    approval_id: int,
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db),
):
    enforce_feature(tenant, "has_ai_features")
    if not tenant.is_admin_or_owner:
        raise HTTPException(status_code=http_status.HTTP_403_FORBIDDEN, detail="Only admins can reject this.")
    approval = await approvals.get_approval(db, tenant.organization_id, approval_id)
    if approval is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Approval request not found.")
    if approval.status != "pending":
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail=f"This request is already {approval.status}.")

    change_set = await change_sets.get_change_set(db, tenant.organization_id, approval.change_set_id)
    if change_set is not None:
        await approvals.reject_approval(db, approval, change_set, tenant.user.id)

    return await _record_action_exchange(
        db, tenant, approval.session_id, "[Rejected by admin]",
        "An admin declined this bulk update — nothing was changed.", [], require_owner=False,
    )
