from fastapi import status as http_status

from app.core.auth_errors import AppException, ErrorDef
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
    """Owner/Admin can invite clients to any project. A Team Manager or a
    Project Manager (role or granted flag) can only invite clients to a
    project they're explicitly assigned to (ProjectMembership) — matches
    app.core.project_access's rule that Team Manager has no organization-
    wide project access, only whatever specific project(s) they've been
    separately given membership on.

    Previously `tenant.is_manager_or_above` alone (true for Team Manager)
    returned early with no project check at all, letting a plain Team
    Manager invite clients to ANY project in the org — a team-scoped role
    has no business managing an arbitrary project's client access."""
    if tenant.is_admin_or_owner:
        return
    if (tenant.is_manager_or_above or tenant.has_project_manager_access) and await project_repo.is_member(project_id, tenant.user.id):
        return
    raise AppException(PROJECT_INVITE_FORBIDDEN)
