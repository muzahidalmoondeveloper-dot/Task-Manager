from datetime import date

from fastapi import APIRouter, Depends
from fastapi import status as http_status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_errors import AppException, ErrorDef
from app.core.database import get_db
from app.core.org_roles import CLIENT, PROJECT_MANAGER
from app.core.project_permissions import require_can_invite_to_project
from app.core.tenant import TenantContext, get_tenant_context, require_org_admin
from app.repositories.onboarding_repository import ClientOnboardingRepository, OnboardingTemplateRepository
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.user_repository import UserRepository
from app.services import onboarding_document_service
from app.schemas.onboarding import (
    ClientOnboardingCreate,
    ClientOnboardingListRead,
    ClientOnboardingRead,
    ClientOnboardingStepRead,
    ClientOnboardingStepUpdate,
    ClientOnboardingUpdate,
    OnboardingTemplateCreate,
    OnboardingTemplateRead,
    OnboardingTemplateUpdate,
)

router = APIRouter(tags=["Client Onboarding"])

_DOCUMENT_SUBDIR = "onboarding_documents"

_TEMPLATE_NOT_FOUND = ErrorDef(code="ONBOARDING_TEMPLATE_NOT_FOUND", status=http_status.HTTP_404_NOT_FOUND, message="Onboarding template not found.")
_ONBOARDING_NOT_FOUND = ErrorDef(code="CLIENT_ONBOARDING_NOT_FOUND", status=http_status.HTTP_404_NOT_FOUND, message="Client onboarding record not found.")
_STEP_NOT_FOUND = ErrorDef(code="ONBOARDING_STEP_NOT_FOUND", status=http_status.HTTP_404_NOT_FOUND, message="Onboarding step not found.")
_INVALID_CLIENT = ErrorDef(code="INVALID_CLIENT", status=http_status.HTTP_400_BAD_REQUEST, message="Selected client is not a valid org member.")
_INVALID_PROJECT = ErrorDef(code="INVALID_PROJECT", status=http_status.HTTP_400_BAD_REQUEST, message="Selected project was not found.")
_ONBOARDING_FORBIDDEN = ErrorDef(code="CLIENT_ONBOARDING_FORBIDDEN", status=http_status.HTTP_403_FORBIDDEN, message="You do not have access to this onboarding record.")
_DUPLICATE_ONBOARDING = ErrorDef(code="DUPLICATE_ACTIVE_ONBOARDING", status=http_status.HTTP_409_CONFLICT, message="This client already has an active onboarding for this project.")
_INVALID_PROJECT_MANAGER = ErrorDef(code="INVALID_PROJECT_MANAGER", status=http_status.HTTP_400_BAD_REQUEST, message="Selected project manager is not a valid Project Manager in this organization.")
_INVALID_DUE_DATE = ErrorDef(code="INVALID_DUE_DATE", status=http_status.HTTP_400_BAD_REQUEST, message="Due date cannot be in the past.")
_INVALID_STATUS_CHANGE = ErrorDef(code="INVALID_ONBOARDING_STATUS", status=http_status.HTTP_400_BAD_REQUEST, message="This onboarding cannot be marked completed until all required steps are done.")


async def _validate_project_manager(org_repo: OrganizationRepository, tenant: TenantContext, project_manager_id: int | None) -> None:
    if project_manager_id is None:
        return
    membership = await org_repo.get_membership(tenant.organization_id, project_manager_id)
    is_valid_pm = membership is not None and membership.is_active and (
        membership.role == PROJECT_MANAGER or membership.is_project_manager
    )
    if not is_valid_pm:
        raise AppException(_INVALID_PROJECT_MANAGER)


def _validate_due_date(due_date) -> None:
    if due_date is not None and due_date < date.today():
        raise AppException(_INVALID_DUE_DATE)


async def _require_onboarding_access(tenant: TenantContext, onboarding) -> None:
    if tenant.is_admin_or_owner:
        return
    if tenant.org_role == PROJECT_MANAGER and onboarding.project_manager_id == tenant.user.id:
        return
    if tenant.org_role == CLIENT and onboarding.client_user_id == tenant.user.id:
        return
    raise AppException(_ONBOARDING_FORBIDDEN)


# Statuses a client may move their own step into via the generic step-update
# endpoint — anything reviewer-only (approved/completed/changes_requested/
# skipped) must come from staff. Actual submission (which validates required
# fields/documents first) goes through the dedicated endpoints in
# onboarding_steps.py, not this generic status setter.
_CLIENT_ALLOWED_STEP_STATUSES = {"in_progress"}


# ── Templates ────────────────────────────────────────────────────────────────

@router.get("/onboarding/templates", response_model=list[OnboardingTemplateRead])
async def list_templates(tenant: TenantContext = Depends(get_tenant_context), db: AsyncSession = Depends(get_db)):
    # Readable by any org member (not just admins) — a Project Manager
    # starting a client onboarding needs to see and preselect the default
    # template too. Only creating/editing/deleting templates stays admin-only.
    repo = OnboardingTemplateRepository(db, tenant.organization_id)
    return await repo.list_all()


@router.post("/onboarding/templates", response_model=OnboardingTemplateRead, status_code=http_status.HTTP_201_CREATED)
async def create_template(
    payload: OnboardingTemplateCreate,
    tenant: TenantContext = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db),
):
    repo = OnboardingTemplateRepository(db, tenant.organization_id)
    return await repo.create(payload, created_by_id=tenant.user.id)


@router.get("/onboarding/templates/{template_id}", response_model=OnboardingTemplateRead)
async def get_template(template_id: int, tenant: TenantContext = Depends(get_tenant_context), db: AsyncSession = Depends(get_db)):
    repo = OnboardingTemplateRepository(db, tenant.organization_id)
    template = await repo.get_by_id(template_id)
    if template is None:
        raise AppException(_TEMPLATE_NOT_FOUND)
    return template


@router.put("/onboarding/templates/{template_id}", response_model=OnboardingTemplateRead)
async def update_template(
    template_id: int,
    payload: OnboardingTemplateUpdate,
    tenant: TenantContext = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db),
):
    repo = OnboardingTemplateRepository(db, tenant.organization_id)
    template = await repo.get_by_id(template_id)
    if template is None:
        raise AppException(_TEMPLATE_NOT_FOUND)
    return await repo.update(template, payload)


@router.delete("/onboarding/templates/{template_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_template(template_id: int, tenant: TenantContext = Depends(require_org_admin), db: AsyncSession = Depends(get_db)):
    repo = OnboardingTemplateRepository(db, tenant.organization_id)
    template = await repo.get_by_id(template_id)
    if template is None:
        raise AppException(_TEMPLATE_NOT_FOUND)
    await repo.delete(template)


@router.post("/onboarding/templates/{template_id}/duplicate", response_model=OnboardingTemplateRead, status_code=http_status.HTTP_201_CREATED)
async def duplicate_template(template_id: int, tenant: TenantContext = Depends(require_org_admin), db: AsyncSession = Depends(get_db)):
    repo = OnboardingTemplateRepository(db, tenant.organization_id)
    template = await repo.get_by_id(template_id)
    if template is None:
        raise AppException(_TEMPLATE_NOT_FOUND)
    return await repo.duplicate(template, created_by_id=tenant.user.id)


# ── Client onboardings ──────────────────────────────────────────────────────

@router.get("/client-onboardings", response_model=list[ClientOnboardingListRead])
async def list_client_onboardings(tenant: TenantContext = Depends(get_tenant_context), db: AsyncSession = Depends(get_db)):
    repo = ClientOnboardingRepository(db, tenant.organization_id)
    if tenant.is_admin_or_owner:
        return await repo.list_all()
    if tenant.org_role == PROJECT_MANAGER:
        return await repo.list_all(project_manager_id=tenant.user.id)
    if tenant.org_role == CLIENT:
        return await repo.list_all(client_user_id=tenant.user.id)
    raise AppException(_ONBOARDING_FORBIDDEN)


@router.post("/client-onboardings", response_model=ClientOnboardingRead, status_code=http_status.HTTP_201_CREATED)
async def create_client_onboarding(
    payload: ClientOnboardingCreate,
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db),
):
    project_repo = ProjectRepository(db, tenant.organization_id)
    project = await project_repo.get_by_id(payload.project_id)
    if project is None:
        raise AppException(_INVALID_PROJECT)

    # Same authorization as inviting a new client (Owner/Admin/Team Manager
    # anywhere, a Project Manager only for a project they're assigned to) —
    # this is the "existing client" branch of the same unified "Start Client
    # Onboarding" action, so both branches share one permission model.
    await require_can_invite_to_project(tenant, project_repo, payload.project_id)

    user_repo = UserRepository(db)
    client_user = await user_repo.get_by_id(payload.client_user_id)
    if client_user is None:
        raise AppException(_INVALID_CLIENT)

    org_repo = OrganizationRepository(db)
    client_membership = await org_repo.get_membership(tenant.organization_id, payload.client_user_id)
    if client_membership is None or not client_membership.is_active:
        raise AppException(_INVALID_CLIENT)

    await _validate_project_manager(org_repo, tenant, payload.project_manager_id)
    _validate_due_date(payload.due_date)

    # No PM explicitly picked → use whoever's already assigned to this
    # project (ProjectMembership) rather than asking the caller to re-pick
    # one the project already has.
    project_manager_id = payload.project_manager_id
    if project_manager_id is None:
        project_manager_id = await project_repo.get_assigned_project_manager_id(payload.project_id)

    template_repo = OnboardingTemplateRepository(db, tenant.organization_id)
    # No explicit template picked → fall back to the org's single default
    # template, same as the client-invitation path.
    template = None
    if payload.template_id is not None:
        template = await template_repo.get_by_id(payload.template_id)
        if template is None:
            raise AppException(_TEMPLATE_NOT_FOUND)
    else:
        template = await template_repo.get_default()

    repo = ClientOnboardingRepository(db, tenant.organization_id)
    existing = await repo.get_active_for_client_project(payload.client_user_id, payload.project_id)
    if existing is not None:
        raise AppException(_DUPLICATE_ONBOARDING, details={"onboarding_id": existing.id})

    # Give the client access to this project the same way accepting an
    # invitation does — without this, the onboarding record is created but
    # the client's own project list/detail view (scoped to ProjectMembership)
    # never surfaces it, and the checklist never renders on their side.
    # Idempotent: no-ops if they're already a member.
    await project_repo.add_member(payload.project_id, payload.client_user_id)

    if project_manager_id != payload.project_manager_id:
        payload = payload.model_copy(update={"project_manager_id": project_manager_id})

    return await repo.create(payload, created_by_id=tenant.user.id, template=template)


@router.get("/client-onboardings/{onboarding_id}", response_model=ClientOnboardingRead)
async def get_client_onboarding(onboarding_id: int, tenant: TenantContext = Depends(get_tenant_context), db: AsyncSession = Depends(get_db)):
    repo = ClientOnboardingRepository(db, tenant.organization_id)
    onboarding = await repo.get_by_id(onboarding_id)
    if onboarding is None:
        raise AppException(_ONBOARDING_NOT_FOUND)
    await _require_onboarding_access(tenant, onboarding)
    return onboarding


@router.delete("/client-onboardings/{onboarding_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_client_onboarding(
    onboarding_id: int,
    tenant: TenantContext = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db),
):
    """Admin-only, hard delete — removes the record and (via cascade) all of
    its steps, responses, documents, and change requests. Any invitation that
    created this onboarding keeps its own history; its onboarding_id just
    goes back to null (ON DELETE SET NULL)."""
    repo = ClientOnboardingRepository(db, tenant.organization_id)
    onboarding = await repo.get_by_id(onboarding_id)
    if onboarding is None:
        raise AppException(_ONBOARDING_NOT_FOUND)

    for step in onboarding.steps:
        for document in step.documents:
            onboarding_document_service.delete_document_file(document.file_url, _DOCUMENT_SUBDIR)

    await repo.delete(onboarding)
    return None


@router.put("/client-onboardings/{onboarding_id}", response_model=ClientOnboardingRead)
async def update_client_onboarding(
    onboarding_id: int,
    payload: ClientOnboardingUpdate,
    tenant: TenantContext = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db),
):
    repo = ClientOnboardingRepository(db, tenant.organization_id)
    onboarding = await repo.get_by_id(onboarding_id)
    if onboarding is None:
        raise AppException(_ONBOARDING_NOT_FOUND)

    org_repo = OrganizationRepository(db)
    if payload.project_manager_id is not None:
        await _validate_project_manager(org_repo, tenant, payload.project_manager_id)
    if payload.due_date is not None:
        _validate_due_date(payload.due_date)
    if payload.status == "completed" and onboarding.progress_percentage < 100:
        raise AppException(_INVALID_STATUS_CHANGE)

    return await repo.update(onboarding, payload)


@router.get("/client-onboardings/audit/inconsistencies")
async def audit_client_onboardings(tenant: TenantContext = Depends(require_org_admin), db: AsyncSession = Depends(get_db)):
    """Read-only diagnostic list of onboarding records whose stored status
    doesn't match what the current rules would derive (duplicate actives,
    stale 'completed' progress, past-due records still active, etc). Never
    modifies anything — flags them for an admin to review and fix by hand."""
    repo = ClientOnboardingRepository(db, tenant.organization_id)
    return await repo.audit_inconsistencies()


@router.post("/client-onboardings/{onboarding_id}/start", response_model=ClientOnboardingRead)
async def start_client_onboarding(onboarding_id: int, tenant: TenantContext = Depends(get_tenant_context), db: AsyncSession = Depends(get_db)):
    repo = ClientOnboardingRepository(db, tenant.organization_id)
    onboarding = await repo.get_by_id(onboarding_id)
    if onboarding is None:
        raise AppException(_ONBOARDING_NOT_FOUND)
    await _require_onboarding_access(tenant, onboarding)
    return await repo.start(onboarding)


# ── Onboarding steps ─────────────────────────────────────────────────────────

@router.get("/client-onboardings/{onboarding_id}/steps", response_model=list[ClientOnboardingStepRead])
async def list_onboarding_steps(onboarding_id: int, tenant: TenantContext = Depends(get_tenant_context), db: AsyncSession = Depends(get_db)):
    repo = ClientOnboardingRepository(db, tenant.organization_id)
    onboarding = await repo.get_by_id(onboarding_id)
    if onboarding is None:
        raise AppException(_ONBOARDING_NOT_FOUND)
    await _require_onboarding_access(tenant, onboarding)
    return onboarding.steps


@router.put("/client-onboardings/{onboarding_id}/steps/{step_id}", response_model=ClientOnboardingStepRead)
async def update_onboarding_step(
    onboarding_id: int,
    step_id: int,
    payload: ClientOnboardingStepUpdate,
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db),
):
    repo = ClientOnboardingRepository(db, tenant.organization_id)
    onboarding = await repo.get_by_id(onboarding_id)
    if onboarding is None:
        raise AppException(_ONBOARDING_NOT_FOUND)
    await _require_onboarding_access(tenant, onboarding)

    step = await repo.get_step(onboarding_id, step_id)
    if step is None:
        raise AppException(_STEP_NOT_FOUND)

    if tenant.org_role == CLIENT and not tenant.is_admin_or_owner:
        # Clients may only move a step through the "I'm working on it /
        # I've submitted it" states — approval, rejection, and skipping are
        # reviewer-only actions that must come from staff.
        if payload.assigned_user_id is not None or payload.due_date is not None:
            raise AppException(_ONBOARDING_FORBIDDEN)
        if payload.status is not None and payload.status not in _CLIENT_ALLOWED_STEP_STATUSES:
            raise AppException(_ONBOARDING_FORBIDDEN)

    return await repo.update_step(onboarding, step, payload)
