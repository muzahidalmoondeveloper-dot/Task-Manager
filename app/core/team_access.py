"""Shared team-scope access control — the team-side counterpart to
app.core.project_access. The single place that decides whether a role is
restricted to only the team(s) it's actually the manager of or a member
of, so every route touching team-scoped data (the team itself, and
everything filed under it — Rocks, Issues, KPIs, Team News, ...) enforces
the identical rule instead of each route re-implementing its own copy.

RULE: Owner/Admin — unrestricted, every team in the org. Everyone else —
including a Team Manager, a Project Manager who's been given access to a
specific team, and a Team Member — only the team(s) they manage
(`Team.team_manager_id`) or have `TeamMembership` on. A Team Manager
therefore has full create/edit/delete/view access to everything under
their own team (they pass this check), and is blocked from every other
team (they don't) — matching app.core.project_access's project-level rule
exactly.
"""

from app.core.auth_errors import AppException, ErrorDef
from app.core.tenant import TenantContext
from app.repositories.team_repository import TeamRepository
from fastapi import status as http_status

NOT_ASSIGNED = ErrorDef(
    code="TEAM_NOT_ASSIGNED",
    status=http_status.HTTP_403_FORBIDDEN,
    message="You are not assigned to this team.",
)


async def require_team_access(tenant: TenantContext, repo: TeamRepository, team_id: int) -> None:
    """Raises TEAM_NOT_ASSIGNED (403) unless `tenant` is Owner/Admin or has
    access (manager or member) to `team_id`. Assumes the caller has already
    confirmed the team exists (a 404 is more informative than a blanket 403
    for a team_id that simply doesn't exist)."""
    if tenant.is_admin_or_owner:
        return
    if not await repo.has_access(team_id, tenant.user.id):
        raise AppException(NOT_ASSIGNED)
