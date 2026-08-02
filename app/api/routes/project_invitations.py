import uuid

from fastapi import APIRouter, Depends
from fastapi import status as http_status

from app.core.auth_errors import AppException, ErrorDef
from app.core.org_roles import CLIENT
from app.core.project_permissions import require_can_invite_to_project
from app.core.tenant import TenantContext, get_tenant_context
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.user_repository import UserRepository
from app.schemas.organization import ClientInvitationRequest, InvitationRead
from app.services.email_service import EmailService

router = APIRouter(prefix="/projects/{project_id}/client-invitations", tags=["Client Invitations"])

_PROJECT_NOT_FOUND = ErrorDef(code="PROJECT_NOT_FOUND", status=http_status.HTTP_404_NOT_FOUND, message="Project not found.")
_NOT_ALLOWED = ErrorDef(code="CLIENT_INVITE_FORBIDDEN", status=http_status.HTTP_403_FORBIDDEN, message="You are not allowed to invite clients to this project.")
_ALREADY_MEMBER = ErrorDef(code="ALREADY_PROJECT_MEMBER", status=http_status.HTTP_400_BAD_REQUEST, message="This person already has access to the project.")
_INVITATION_NOT_FOUND = ErrorDef(code="INVITATION_NOT_FOUND", status=http_status.HTTP_404_NOT_FOUND, message="Invitation not found.")


@router.post("", response_model=InvitationRead, status_code=http_status.HTTP_201_CREATED)
async def invite_client(
    project_id: int,
    payload: ClientInvitationRequest,
    tenant: TenantContext = Depends(get_tenant_context),
):
    project_repo = ProjectRepository(tenant.db, tenant.organization_id)
    project = await project_repo.get_by_id(project_id)
    if project is None:
        raise AppException(_PROJECT_NOT_FOUND)

    await require_can_invite_to_project(tenant, project_repo, project_id)

    email = str(payload.email).lower().strip()
    user_repo = UserRepository(tenant.db)
    existing_user = await user_repo.get_by_email(email)
    if existing_user is not None and await project_repo.is_member(project_id, existing_user.id):
        raise AppException(_ALREADY_MEMBER)

    org_repo = OrganizationRepository(tenant.db)
    invitation = await org_repo.create_invitation(
        org_id=tenant.organization_id,
        email=email,
        role=CLIENT,
        invited_by_id=tenant.user.id,
        project_id=project_id,
    )
    await tenant.db.commit()
    await tenant.db.refresh(invitation)

    email_service = EmailService()
    email_service.send_client_invitation_email(
        to_email=email,
        org_name=tenant.organization.name,
        project_name=project.name,
        inviter_name=tenant.user.full_name,
        token=invitation.token,
    )

    return InvitationRead.model_validate(invitation)


@router.get("", response_model=list[InvitationRead])
async def list_client_invitations(
    project_id: int,
    tenant: TenantContext = Depends(get_tenant_context),
):
    project_repo = ProjectRepository(tenant.db, tenant.organization_id)
    if await project_repo.get_by_id(project_id) is None:
        raise AppException(_PROJECT_NOT_FOUND)
    await require_can_invite_to_project(tenant, project_repo, project_id)

    org_repo = OrganizationRepository(tenant.db)
    pending = await org_repo.list_pending_invitations(tenant.organization_id)
    return [InvitationRead.model_validate(i) for i in pending if i.project_id == project_id]


@router.delete("/{invitation_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def revoke_client_invitation(
    project_id: int,
    invitation_id: uuid.UUID,
    tenant: TenantContext = Depends(get_tenant_context),
):
    project_repo = ProjectRepository(tenant.db, tenant.organization_id)
    if await project_repo.get_by_id(project_id) is None:
        raise AppException(_PROJECT_NOT_FOUND)
    await require_can_invite_to_project(tenant, project_repo, project_id)

    org_repo = OrganizationRepository(tenant.db)
    invitation = await org_repo.get_invitation_by_id(tenant.organization_id, invitation_id)
    if invitation is None or invitation.project_id != project_id or invitation.accepted_at is not None:
        raise AppException(_INVITATION_NOT_FOUND)

    await tenant.db.delete(invitation)
    await tenant.db.commit()
    return None
