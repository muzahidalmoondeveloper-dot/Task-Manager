from fastapi import status as http_status

from app.core.auth_errors import AppException, ErrorDef
from app.core.org_roles import PROJECT_MANAGER
from app.core.tenant import TenantContext
from app.repositories.project_repository import ProjectRepository

PROJECT_INVITE_FORBIDDEN = ErrorDef(
    code="CLIENT_INVITE_FORBIDDEN",
    status=http_status.HTTP_403_FORBIDDEN,
    message="You are not allowed to invite clients to this project.",
)


async def require_can_invite_to_project(
    tenant: TenantContext, project_repo: ProjectRepository, project_id: int
) -> None:
    """Owner/Admin/Team Manager can invite clients to any project; a Project
    Manager can only invite clients to a project they're assigned to."""
    if tenant.is_manager_or_above:
        return
    if tenant.org_role == PROJECT_MANAGER and await project_repo.is_member(project_id, tenant.user.id):
        return
    raise AppException(PROJECT_INVITE_FORBIDDEN)
