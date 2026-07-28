from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi import status as http_status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.tenant import TenantContext, enforce_feature, get_tenant_context
from app.repositories.chat_repository import ChatRepository
from app.schemas.chat import (
    ChatMessageRequest,
    ChatMessageResponse,
    ChatSessionRead,
    ChatSessionWithMessages,
)
from app.services.chat_service import ChatService
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
