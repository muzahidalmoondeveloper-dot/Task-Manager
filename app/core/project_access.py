"""Shared project-scope access control — the single place that decides
whether a role is restricted to only the projects it's explicitly assigned
to (`ProjectMembership`), so every route touching project-scoped data
(projects themselves, their tasks, their reports, ...) enforces the
identical rule instead of each route re-implementing (or, worse, forgetting
to implement) its own copy.

RULE (current product decision — supersedes the earlier "team-manager
privileges unlock everything" broadening):
- Owner/Admin: unrestricted, every project in the org.
- Team Manager (base role, OR anyone granted the `is_team_manager`
  privilege flag) has NO project access by default. If they're separately
  assigned as the Project Manager of a specific project (a normal
  `ProjectMembership` row, exactly like assigning any other Project
  Manager), they can view/access exactly that project — nothing broader.
- Project Manager (base role, OR anyone granted the `is_project_manager`
  privilege flag): view/access only the project(s) they're a member of.
  Being additionally granted access to a specific Team is a separate,
  additive grant handled by team-membership checks, not this module.
- Client: view/access only the project(s) they're a member of (unchanged).
- Team Member (no elevated flags): unrestricted org-wide project
  visibility (unchanged) — not part of this restriction; team members
  don't manage projects, they're just assigned tasks within them.
"""

from app.core.auth_errors import AppException, ErrorDef
from app.core.org_roles import CLIENT
from app.core.tenant import TenantContext
from app.repositories.project_repository import ProjectRepository
from fastapi import status as http_status

NOT_ASSIGNED = ErrorDef(
    code="PROJECT_NOT_ASSIGNED",
    status=http_status.HTTP_403_FORBIDDEN,
    message="You are not assigned to this project.",
)


def is_project_scoped(tenant: TenantContext) -> bool:
    """True for anyone who must be restricted to only their
    ProjectMembership-assigned project(s): Team Managers (role or granted
    privilege flag), Project Managers (role or granted privilege flag), and
    Clients. False for Owner/Admin (unrestricted) and plain Team Members
    (unaffected by this rule)."""
    if tenant.is_admin_or_owner:
        return False
    if tenant.is_manager_or_above or tenant.has_project_manager_access:
        return True
    return tenant.org_role == CLIENT


async def require_project_access(tenant: TenantContext, repo: ProjectRepository, project_id: int) -> None:
    """Raises PROJECT_NOT_ASSIGNED (403) if `tenant` is project-scoped and
    isn't a member of `project_id`. A no-op for every other role."""
    if is_project_scoped(tenant) and not await repo.is_member(project_id, tenant.user.id):
        raise AppException(NOT_ASSIGNED)
