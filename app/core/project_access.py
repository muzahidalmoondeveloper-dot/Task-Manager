"""Shared project-scope access control — the single place that decides
whether a role is restricted to only the projects it's explicitly assigned
to (`ProjectMembership`), so every route touching project-scoped data
(projects themselves, their tasks, their reports, ...) enforces the
identical rule instead of each route re-implementing (or, worse, forgetting
to implement) its own copy.

RULE (current product decision):
- Owner/Admin: unrestricted, every project in the org.
- Project Manager (base role, OR anyone granted the `is_project_manager`
  privilege flag): view/access only the project(s) they're a member of
  (`ProjectMembership`).
- Client: view/access only the project(s) they're a member of (unchanged).
- Team Member (no elevated flags): unrestricted org-wide project
  visibility (unchanged) — not part of this restriction; team members
  don't manage projects, they're just assigned tasks within them.
- Team Manager (base role, OR anyone granted the `is_team_manager`
  privilege flag) is a team-scoped role and has NO Projects-management
  access via this module, full stop — NOT even a project they happen to
  hold a `ProjectMembership` row on. `ProjectMembership` alone is never
  sufficient; it must be paired with explicit Project Manager capability
  (`has_project_manager_access`) or Admin. A Team Manager who is ALSO been
  separately granted Project Manager capability gets project access
  through that grant, exactly like any other Project Manager — never
  through being a Team Manager. (Supersedes an earlier version of this
  rule that treated Team Manager the same as Project Manager here — that
  let a plain Team Manager's own `ProjectMembership` row substitute for
  explicit Project Manager capability, which the current model forbids.)

`is_project_scoped()`/`require_project_access()` below keep their original
meaning and are still used as-is by app.api.routes.tasks.py for task<->
project linkage (a Team Manager creating/listing tasks tagged to a project
they're a ProjectMembership member of) — that's a distinct, narrower
concern from "may this user use the Projects management system" and is
unaffected by the Team-Manager-lockout rule above. That stricter rule is
enforced separately by `is_project_management_blocked()`/
`require_project_management_access()` further down, used only by
app.api.routes.projects.py's actual project list/detail/edit routes.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
    (unaffected by this rule).

    NOTE: kept exactly as originally defined — still used by
    app.api.routes.tasks.py for task<->project linkage. Do NOT use this to
    decide whether someone may access the *Projects management system*
    itself; use is_project_management_blocked()/
    require_project_management_access() below for that, since a plain Team
    Manager must be blocked from that surface outright, not merely
    membership-scoped."""
    if tenant.is_admin_or_owner:
        return False
    if tenant.is_manager_or_above or tenant.has_project_manager_access:
        return True
    return tenant.org_role == CLIENT


async def require_project_access(tenant: TenantContext, repo: ProjectRepository, project_id: int) -> None:
    """Raises PROJECT_NOT_ASSIGNED (403) if `tenant` is project-scoped and
    isn't a member of `project_id`. A no-op for every other role.

    NOTE: same scope caveat as is_project_scoped() above — this is the
    task<->project linkage rule, not the Projects-management-system rule."""
    if is_project_scoped(tenant) and not await repo.is_member(project_id, tenant.user.id):
        raise AppException(NOT_ASSIGNED)


def is_project_management_blocked(tenant: TenantContext) -> bool:
    """True only for a Team Manager (role or granted `is_team_manager`
    flag) who has NOT also been separately granted Project Manager
    capability or Admin — i.e. someone whose only path to project access
    would otherwise be "I happen to hold a ProjectMembership row," which
    this model explicitly forbids as sufficient. Owner/Admin and anyone
    with `has_project_manager_access` are never blocked here (Admin/Owner
    are unrestricted via is_project_scoped() below; a genuine Project
    Manager, including a Team-Manager-who's-also-a-Project-Manager, is
    membership-scoped via is_project_scoped() exactly as before)."""
    if tenant.is_admin_or_owner or tenant.has_project_manager_access:
        return False
    return tenant.is_manager_or_above  # true here only via the Team Manager branch — Admin/Owner already excluded above


async def require_project_management_access(tenant: TenantContext, repo: ProjectRepository, project_id: int) -> None:
    """The Projects-management-system counterpart to require_project_access
    above: a plain Team Manager (no Project Manager/Admin capability) is
    rejected outright, before any ProjectMembership lookup even happens —
    a ProjectMembership row must never substitute for explicit Project
    Manager capability on this surface. Everyone else defers to
    require_project_access()'s existing, unchanged rule (Owner/Admin
    unrestricted; genuine Project Manager/Client membership-scoped)."""
    if is_project_management_blocked(tenant):
        raise AppException(NOT_ASSIGNED)
    await require_project_access(tenant, repo, project_id)


async def list_project_team_ids(db: AsyncSession, organization_id: uuid.UUID, project_id: int) -> set[int]:
    """Every team currently associated with `project_id` — via its Rocks
    (including Rocks under the project's own Objectives), the KPIs tracking
    those Rocks, the project's own KPIs, and its Tasks. There is no explicit
    Project<->Team membership table in this schema, so this mirrors the
    derivation GET /projects/{id}/items already uses (inline there, since
    it also needs the underlying Rock/KPI objects themselves for its other
    response fields — this copy exists so task-creation's write-side
    team_id validation can enforce the identical rule without requiring
    that route's full object graph). Callers already own tenant/project-
    access checks; this only computes the team id set — it does not
    authorize the request."""
    from app.models.kpi import KPI
    from app.models.objective import Objective
    from app.models.rock import Rock
    from app.models.task import Task

    rocks_result = await db.execute(
        select(Rock.id, Rock.team_id).where(Rock.project_id == project_id, Rock.organization_id == organization_id)
    )
    rocks = rocks_result.all()
    team_ids = {row.team_id for row in rocks if row.team_id is not None}

    kpis_result = await db.execute(
        select(KPI.team_id).where(KPI.project_id == project_id, KPI.organization_id == organization_id)
    )
    team_ids |= {row[0] for row in kpis_result.all() if row[0] is not None}

    objectives_result = await db.execute(
        select(Objective.id).where(Objective.project_id == project_id, Objective.organization_id == organization_id)
    )
    objective_ids = [row[0] for row in objectives_result.all()]

    objective_rock_ids: list[int] = []
    if objective_ids:
        objective_rocks_result = await db.execute(
            select(Rock.id, Rock.team_id).where(Rock.objective_id.in_(objective_ids), Rock.organization_id == organization_id)
        )
        objective_rocks = objective_rocks_result.all()
        objective_rock_ids = [row.id for row in objective_rocks]
        team_ids |= {row.team_id for row in objective_rocks if row.team_id is not None}

    all_rock_ids = [row.id for row in rocks] + objective_rock_ids
    if all_rock_ids:
        rock_kpis_result = await db.execute(
            select(KPI.team_id).where(KPI.rock_id.in_(all_rock_ids), KPI.organization_id == organization_id)
        )
        team_ids |= {row[0] for row in rock_kpis_result.all() if row[0] is not None}

    tasks_result = await db.execute(
        select(Task.team_id).where(Task.project_id == project_id, Task.organization_id == organization_id)
    )
    team_ids |= {row[0] for row in tasks_result.all() if row[0] is not None}

    return team_ids
