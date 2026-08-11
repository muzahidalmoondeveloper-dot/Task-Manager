import uuid

from fastapi import APIRouter, Depends
from fastapi import status as http_status

from app.core.auth_errors import AppException, ErrorDef
from app.core.org_roles import CLIENT
from app.core.project_permissions import require_can_invite_to_project
from app.core.tenant import TenantContext, get_tenant_context
from app.repositories.onboarding_repository import ClientOnboardingRepository, OnboardingTemplateRepository
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.user_repository import UserRepository
from app.schemas.organization import ClientInvitationCreate, ClientInvitationRead
from app.services.email_service import EmailService

router = APIRouter(prefix="/client-invitations", tags=["Client Invitations"])

_PROJECT_NOT_FOUND = ErrorDef(code="PROJECT_NOT_FOUND", status=http_status.HTTP_404_NOT_FOUND, message="Project not found.")
_TEMPLATE_NOT_FOUND = ErrorDef(code="ONBOARDING_TEMPLATE_NOT_FOUND", status=http_status.HTTP_404_NOT_FOUND, message="Onboarding template not found.")
_TEMPLATE_REQUIRED = ErrorDef(code="ONBOARDING_TEMPLATE_REQUIRED", status=http_status.HTTP_422_UNPROCESSABLE_ENTITY, message="Create or configure an onboarding template first.")
_INVALID_PM = ErrorDef(code="INVALID_PROJECT_MANAGER", status=http_status.HTTP_400_BAD_REQUEST, message="Selected project manager is not a valid org member.")
_ALREADY_MEMBER = ErrorDef(code="ALREADY_PROJECT_MEMBER", status=http_status.HTTP_400_BAD_REQUEST, message="This person already has access to the project.")
_DUPLICATE_ONBOARDING = ErrorDef(code="DUPLICATE_ACTIVE_ONBOARDING", status=http_status.HTTP_409_CONFLICT, message="This client already has an active onboarding for this project.")
_DUPLICATE_INVITATION = ErrorDef(code="DUPLICATE_INVITATION", status=http_status.HTTP_400_BAD_REQUEST, message="There is already an active invitation for this email on this project.")
_INVITATION_NOT_FOUND = ErrorDef(code="INVITATION_NOT_FOUND", status=http_status.HTTP_404_NOT_FOUND, message="Invitation not found.")
_INVITATION_LOCKED = ErrorDef(code="INVITATION_LOCKED", status=http_status.HTTP_400_BAD_REQUEST, message="This invitation can no longer be modified.")
_NOT_A_DRAFT = ErrorDef(code="INVITATION_NOT_A_DRAFT", status=http_status.HTTP_400_BAD_REQUEST, message="Only draft invitations can be deleted directly — revoke a sent invitation instead.")


async def _validate_and_load(tenant: TenantContext, payload: ClientInvitationCreate) -> tuple:
    project_repo = ProjectRepository(tenant.db, tenant.organization_id)
    project = await project_repo.get_by_id(payload.project_id)
    if project is None:
        raise AppException(_PROJECT_NOT_FOUND)

    await require_can_invite_to_project(tenant, project_repo, payload.project_id)

    project_manager_id = payload.project_manager_id
    if project_manager_id is not None:
        org_repo = OrganizationRepository(tenant.db)
        pm_membership = await org_repo.get_membership(tenant.organization_id, project_manager_id)
        if pm_membership is None or not pm_membership.is_active:
            raise AppException(_INVALID_PM)
    else:
        # No PM explicitly picked → use whoever's already assigned to this
        # project (ProjectMembership) rather than asking the caller to
        # re-pick one the project already has.
        project_manager_id = await project_repo.get_assigned_project_manager_id(payload.project_id)

    # If the caller didn't explicitly pick a template, fall back to the org's
    # single active default template (see OnboardingTemplateRepository) —
    # every org has exactly one once it's configured. A real, non-empty
    # template is mandatory: an invitation that can't ultimately produce a
    # real onboarding checklist is rejected up front rather than accepted
    # and left to fail (or silently create a blank onboarding) later at
    # accept-invitation time.
    template_id = payload.onboarding_template_id
    template_repo = OnboardingTemplateRepository(tenant.db, tenant.organization_id)
    if template_id is not None:
        template = await template_repo.get_by_id(template_id)
        if template is None:
            raise AppException(_TEMPLATE_NOT_FOUND)
    else:
        template = await template_repo.get_default()
        template_id = template.id if template else None

    if template is None or not template.steps:
        raise AppException(_TEMPLATE_REQUIRED)

    return project_repo, project, template_id, project_manager_id


@router.post("", response_model=ClientInvitationRead, status_code=http_status.HTTP_201_CREATED)
async def invite_client(
    payload: ClientInvitationCreate,
    tenant: TenantContext = Depends(get_tenant_context),
):
    project_repo, project, template_id, project_manager_id = await _validate_and_load(tenant, payload)

    email = str(payload.email).lower().strip()
    user_repo = UserRepository(tenant.db)
    existing_user = await user_repo.get_by_email(email)
    if existing_user is not None:
        if await project_repo.is_member(payload.project_id, existing_user.id):
            raise AppException(_ALREADY_MEMBER)
        # Inviting an existing client back to a project they already have a
        # live onboarding on would create a second one on acceptance —
        # block it here rather than relying on the accept-time reuse check.
        onboarding_repo = ClientOnboardingRepository(tenant.db, tenant.organization_id)
        existing_onboarding = await onboarding_repo.get_active_for_client_project(existing_user.id, payload.project_id)
        if existing_onboarding is not None:
            raise AppException(_DUPLICATE_ONBOARDING, details={"onboarding_id": existing_onboarding.id})

    org_repo = OrganizationRepository(tenant.db)
    duplicate = await org_repo.get_active_client_invitation(tenant.organization_id, email, payload.project_id)
    if duplicate is not None:
        raise AppException(_DUPLICATE_INVITATION)

    invitation = await org_repo.create_client_invitation(
        org_id=tenant.organization_id,
        email=email,
        invited_by_id=tenant.user.id,
        project_id=payload.project_id,
        client_name=payload.client_name,
        company_name=payload.company_name,
        phone_number=payload.phone_number,
        project_manager_id=project_manager_id,
        onboarding_template_id=template_id,
        due_date=payload.due_date,
        message=payload.message,
        expires_in_days=payload.expires_in_days,
        save_as_draft=payload.save_as_draft,
    )
    await tenant.db.commit()
    invitation = await org_repo.get_client_invitation_by_id(tenant.organization_id, invitation.id)

    if not payload.save_as_draft:
        EmailService().send_client_invitation_email(
            to_email=email,
            org_name=tenant.organization.name,
            project_name=project.name,
            inviter_name=tenant.user.full_name,
            token=invitation.token,
            client_name=payload.client_name,
            message=payload.message,
            template_name=invitation.onboarding_template.name if invitation.onboarding_template else None,
        )

    return ClientInvitationRead.model_validate(invitation)


@router.get("", response_model=list[ClientInvitationRead])
async def list_client_invitations(
    project_id: int | None = None,
    tenant: TenantContext = Depends(get_tenant_context),
):
    org_repo = OrganizationRepository(tenant.db)
    invitations = await org_repo.list_client_invitations(tenant.organization_id)

    if not tenant.is_manager_or_above:
        # A Project Manager only sees invitations for projects they're
        # assigned to (or that they personally sent).
        project_repo = ProjectRepository(tenant.db, tenant.organization_id)
        visible = []
        for inv in invitations:
            if inv.invited_by_id == tenant.user.id or await project_repo.is_member(inv.project_id, tenant.user.id):
                visible.append(inv)
        invitations = visible

    if project_id is not None:
        invitations = [i for i in invitations if i.project_id == project_id]

    return [ClientInvitationRead.model_validate(i) for i in invitations]


@router.post("/{invitation_id}/resend", response_model=ClientInvitationRead)
async def resend_client_invitation(
    invitation_id: uuid.UUID,
    tenant: TenantContext = Depends(get_tenant_context),
):
    org_repo = OrganizationRepository(tenant.db)
    invitation = await org_repo.get_client_invitation_by_id(tenant.organization_id, invitation_id)
    if invitation is None:
        raise AppException(_INVITATION_NOT_FOUND)

    project_repo = ProjectRepository(tenant.db, tenant.organization_id)
    await require_can_invite_to_project(tenant, project_repo, invitation.project_id)

    if invitation.status in ("accepted", "revoked"):
        raise AppException(_INVITATION_LOCKED)

    await org_repo.rotate_invitation_token(invitation)
    await tenant.db.commit()
    invitation = await org_repo.get_client_invitation_by_id(tenant.organization_id, invitation_id)

    EmailService().send_client_invitation_email(
        to_email=invitation.email,
        org_name=tenant.organization.name,
        project_name=invitation.project.name if invitation.project else "",
        inviter_name=tenant.user.full_name,
        token=invitation.token,
        client_name=invitation.client_name,
        message=invitation.message,
        template_name=invitation.onboarding_template.name if invitation.onboarding_template else None,
    )

    return ClientInvitationRead.model_validate(invitation)


@router.post("/{invitation_id}/revoke", response_model=ClientInvitationRead)
async def revoke_client_invitation(
    invitation_id: uuid.UUID,
    tenant: TenantContext = Depends(get_tenant_context),
):
    org_repo = OrganizationRepository(tenant.db)
    invitation = await org_repo.get_client_invitation_by_id(tenant.organization_id, invitation_id)
    if invitation is None:
        raise AppException(_INVITATION_NOT_FOUND)

    project_repo = ProjectRepository(tenant.db, tenant.organization_id)
    await require_can_invite_to_project(tenant, project_repo, invitation.project_id)

    if invitation.status in ("accepted", "revoked"):
        raise AppException(_INVITATION_LOCKED)

    await org_repo.revoke_invitation_soft(invitation)
    await tenant.db.commit()
    invitation = await org_repo.get_client_invitation_by_id(tenant.organization_id, invitation_id)
    return ClientInvitationRead.model_validate(invitation)


@router.delete("/{invitation_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_draft_invitation(
    invitation_id: uuid.UUID,
    tenant: TenantContext = Depends(get_tenant_context),
):
    org_repo = OrganizationRepository(tenant.db)
    invitation = await org_repo.get_client_invitation_by_id(tenant.organization_id, invitation_id)
    if invitation is None:
        raise AppException(_INVITATION_NOT_FOUND)

    project_repo = ProjectRepository(tenant.db, tenant.organization_id)
    await require_can_invite_to_project(tenant, project_repo, invitation.project_id)

    if invitation.status in ("sent", "opened"):
        raise AppException(_NOT_A_DRAFT)

    await org_repo.delete_invitation(invitation)
    await tenant.db.commit()
    return None
