from fastapi import APIRouter, Depends, Form, UploadFile
from fastapi import File as FastAPIFile
from fastapi import status as http_status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.onboarding import (
    _ONBOARDING_FORBIDDEN,
    _ONBOARDING_NOT_FOUND,
    _STEP_NOT_FOUND,
    _require_onboarding_access,
)
from app.core.auth_errors import AppException, ErrorDef
from app.core.database import get_db
from app.core.onboarding_constants import STEP_STATUSES_CLIENT_EDITABLE
from app.core.org_roles import CLIENT, PROJECT_MANAGER
from app.core.tenant import TenantContext, get_tenant_context
from app.repositories.onboarding_repository import ClientOnboardingRepository
from app.schemas.onboarding import (
    ChangeRequestCreate,
    ClientOnboardingStepRead,
    DocumentRead,
    DocumentReviewRequest,
    FormResponsesSaveRequest,
)
from app.services import onboarding_document_service

router = APIRouter(tags=["Client Onboarding — Step Content"])

_DOCUMENT_NOT_FOUND = ErrorDef(code="ONBOARDING_DOCUMENT_NOT_FOUND", status=http_status.HTTP_404_NOT_FOUND, message="Document not found.")
_REQUIREMENT_NOT_FOUND = ErrorDef(code="ONBOARDING_REQUIREMENT_NOT_FOUND", status=http_status.HTTP_404_NOT_FOUND, message="Document requirement not found.")
_STEP_LOCKED = ErrorDef(code="ONBOARDING_STEP_LOCKED", status=http_status.HTTP_400_BAD_REQUEST, message="This step has already been submitted and can no longer be edited.")
_REVIEWER_ONLY = ErrorDef(code="ONBOARDING_REVIEWER_ONLY", status=http_status.HTTP_403_FORBIDDEN, message="Only an admin or the assigned Project Manager can review this step.")
_CLIENT_ONLY = ErrorDef(code="ONBOARDING_CLIENT_ONLY", status=http_status.HTTP_403_FORBIDDEN, message="Only the client can submit their own onboarding.")

_DOCUMENT_SUBDIR = "onboarding_documents"


async def _load_onboarding_and_step(db: AsyncSession, org_id, onboarding_id: int, step_id: int):
    repo = ClientOnboardingRepository(db, org_id)
    onboarding = await repo.get_by_id(onboarding_id)
    if onboarding is None:
        raise AppException(_ONBOARDING_NOT_FOUND)
    step = await repo.get_step(onboarding_id, step_id)
    if step is None:
        raise AppException(_STEP_NOT_FOUND)
    return repo, onboarding, step


def _require_client_owner(tenant: TenantContext, onboarding) -> None:
    if tenant.org_role != CLIENT or onboarding.client_user_id != tenant.user.id:
        raise AppException(_CLIENT_ONLY)


def _require_reviewer(tenant: TenantContext, onboarding) -> None:
    if tenant.is_admin_or_owner:
        return
    if tenant.org_role == PROJECT_MANAGER and onboarding.project_manager_id == tenant.user.id:
        return
    raise AppException(_REVIEWER_ONLY)


def _require_step_editable(step) -> None:
    if step.status not in STEP_STATUSES_CLIENT_EDITABLE:
        raise AppException(_STEP_LOCKED)


# ── Form responses (information_form / questionnaire) ───────────────────────

@router.put(
    "/client-onboardings/{onboarding_id}/steps/{step_id}/responses",
    response_model=ClientOnboardingStepRead,
)
async def save_step_responses(
    onboarding_id: int,
    step_id: int,
    payload: FormResponsesSaveRequest,
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db),
):
    repo, onboarding, step = await _load_onboarding_and_step(db, tenant.organization_id, onboarding_id, step_id)
    _require_client_owner(tenant, onboarding)
    _require_step_editable(step)

    valid_field_ids = {f.id for f in step.form_fields}
    for item in payload.responses:
        if item.field_id not in valid_field_ids:
            raise AppException(_STEP_NOT_FOUND, message="One of the submitted fields does not belong to this step.")

    return await repo.save_responses(onboarding, step, payload, client_user_id=tenant.user.id)


# ── Generic submit (manual_task / meeting / agreement / etc.) ──────────────

@router.post(
    "/client-onboardings/{onboarding_id}/steps/{step_id}/submit",
    response_model=ClientOnboardingStepRead,
)
async def submit_step(
    onboarding_id: int,
    step_id: int,
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db),
):
    repo, onboarding, step = await _load_onboarding_and_step(db, tenant.organization_id, onboarding_id, step_id)
    _require_client_owner(tenant, onboarding)
    _require_step_editable(step)
    return await repo.submit_step(onboarding, step)


# ── Documents ────────────────────────────────────────────────────────────────

@router.post(
    "/client-onboardings/{onboarding_id}/steps/{step_id}/documents",
    response_model=DocumentRead,
    status_code=http_status.HTTP_201_CREATED,
)
async def upload_step_document(
    onboarding_id: int,
    step_id: int,
    requirement_id: int | None = Form(default=None),
    file: UploadFile = FastAPIFile(...),
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db),
):
    repo, onboarding, step = await _load_onboarding_and_step(db, tenant.organization_id, onboarding_id, step_id)
    _require_client_owner(tenant, onboarding)
    _require_step_editable(step)

    requirement = None
    if requirement_id is not None:
        requirement = await repo.get_document_requirement(step, requirement_id)
        if requirement is None:
            raise AppException(_REQUIREMENT_NOT_FOUND)

    allowed_types = requirement.allowed_file_types if requirement else None
    max_mb = requirement.max_file_size_mb if requirement else None
    file_url, extension, size = await onboarding_document_service.save_document(
        file, _DOCUMENT_SUBDIR, onboarding_id,
        allowed_file_types=allowed_types, max_file_size_mb=max_mb,
    )

    return await repo.add_document(
        step, requirement,
        file_name=file.filename or f"document.{extension}",
        file_url=file_url,
        file_type=extension,
        file_size=size,
        uploaded_by_id=tenant.user.id,
    )


@router.post("/onboarding-documents/{document_id}/review", response_model=DocumentRead)
async def review_document(
    document_id: int,
    payload: DocumentReviewRequest,
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db),
):
    repo = ClientOnboardingRepository(db, tenant.organization_id)
    document = await repo.get_document(document_id)
    if document is None:
        raise AppException(_DOCUMENT_NOT_FOUND)

    step = await repo.get_step_by_id(document.client_onboarding_step_id)
    if step is None:
        raise AppException(_STEP_NOT_FOUND)

    onboarding = await repo.get_by_id(step.client_onboarding_id)
    if onboarding is None:
        raise AppException(_ONBOARDING_NOT_FOUND)

    _require_reviewer(tenant, onboarding)
    return await repo.review_document(document, payload, reviewer_id=tenant.user.id)


# ── Review workflow ──────────────────────────────────────────────────────────

@router.post(
    "/client-onboardings/{onboarding_id}/steps/{step_id}/approve",
    response_model=ClientOnboardingStepRead,
)
async def approve_step(
    onboarding_id: int,
    step_id: int,
    comment: str | None = None,
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db),
):
    repo, onboarding, step = await _load_onboarding_and_step(db, tenant.organization_id, onboarding_id, step_id)
    _require_reviewer(tenant, onboarding)
    return await repo.approve_step(onboarding, step, reviewer_id=tenant.user.id, comment=comment)


@router.post(
    "/client-onboardings/{onboarding_id}/steps/{step_id}/request-changes",
    response_model=ClientOnboardingStepRead,
)
async def request_step_changes(
    onboarding_id: int,
    step_id: int,
    payload: ChangeRequestCreate,
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db),
):
    repo, onboarding, step = await _load_onboarding_and_step(db, tenant.organization_id, onboarding_id, step_id)
    _require_reviewer(tenant, onboarding)
    return await repo.request_step_changes(onboarding, step, reason=payload.reason, reviewer_id=tenant.user.id)
