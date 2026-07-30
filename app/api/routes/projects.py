from fastapi import APIRouter, Depends
from fastapi import status as http_status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.routes.teams import _serialize as serialize_team
from app.core.auth_errors import AppException, ErrorDef
from app.core.database import get_db
from app.core.org_roles import CLIENT, PROJECT_MANAGER
from app.core.tenant import TenantContext, get_tenant_context, require_org_admin, require_org_manager
from app.models.issue import Issue
from app.models.kpi import KPI
from app.models.objective import Objective
from app.models.organization import OrganizationMembership
from app.models.rock import Rock
from app.models.task import Task
from app.repositories.project_repository import ProjectRepository
from app.repositories.team_repository import TeamRepository
from app.schemas.issue import IssueOut
from app.schemas.kpi import KPIOut
from app.schemas.org import ObjectiveRead
from app.schemas.project import (
    ProjectCreate,
    ProjectMemberAssign,
    ProjectMemberOut,
    ProjectRead,
    ProjectUpdate,
)
from app.schemas.rock import RockOut

router = APIRouter(prefix="/projects", tags=["Projects"])

_NOT_FOUND = ErrorDef(code="PROJECT_NOT_FOUND", status=http_status.HTTP_404_NOT_FOUND, message="Project not found.")
_PLAN_LIMIT = ErrorDef(code="PLAN_LIMIT_EXCEEDED", status=http_status.HTTP_402_PAYMENT_REQUIRED, message="Your plan's project limit has been reached.")
_NOT_ASSIGNED = ErrorDef(code="PROJECT_NOT_ASSIGNED", status=http_status.HTTP_403_FORBIDDEN, message="You are not assigned to this project.")
_CLIENT_FORBIDDEN = ErrorDef(code="CLIENT_ITEMS_FORBIDDEN", status=http_status.HTTP_403_FORBIDDEN, message="Clients view project progress through Reports, not this endpoint.")


_PROJECT_SCOPED_ROLES = {PROJECT_MANAGER, CLIENT}


async def _require_project_access(tenant: TenantContext, repo: ProjectRepository, project_id: int) -> None:
    """Project Managers and Clients only see projects they're assigned to;
    every other role keeps today's org-wide project visibility."""
    if tenant.org_role in _PROJECT_SCOPED_ROLES and not await repo.is_member(project_id, tenant.user.id):
        raise AppException(_NOT_ASSIGNED)


@router.get("", response_model=list[ProjectRead])
async def list_projects(tenant: TenantContext = Depends(get_tenant_context)):
    repo = ProjectRepository(tenant.db, tenant.organization_id)
    if tenant.org_role in _PROJECT_SCOPED_ROLES:
        return [ProjectRead.model_validate(p) for p in await repo.list_for_user(tenant.user.id)]
    return [ProjectRead.model_validate(p) for p in await repo.list_all()]


@router.post("", response_model=ProjectRead, status_code=http_status.HTTP_201_CREATED)
async def create_project(
    payload: ProjectCreate,
    tenant: TenantContext = Depends(require_org_manager),
):
    limits = tenant.plan_limits
    if limits.max_projects != -1:
        repo_check = ProjectRepository(tenant.db, tenant.organization_id)
        projects = await repo_check.list_all()
        if len(projects) >= limits.max_projects:
            raise AppException(_PLAN_LIMIT, details={"limit": limits.max_projects})

    repo = ProjectRepository(tenant.db, tenant.organization_id)
    project = await repo.create(payload, created_by_id=tenant.user.id)
    return ProjectRead.model_validate(project)


@router.get("/{project_id}", response_model=ProjectRead)
async def get_project(project_id: int, tenant: TenantContext = Depends(get_tenant_context)):
    repo = ProjectRepository(tenant.db, tenant.organization_id)
    project = await repo.get_by_id(project_id)
    if project is None:
        raise AppException(_NOT_FOUND)
    await _require_project_access(tenant, repo, project_id)
    return ProjectRead.model_validate(project)


@router.patch("/{project_id}", response_model=ProjectRead)
async def update_project(
    project_id: int,
    payload: ProjectUpdate,
    tenant: TenantContext = Depends(require_org_manager),
):
    repo = ProjectRepository(tenant.db, tenant.organization_id)
    project = await repo.get_by_id(project_id)
    if project is None:
        raise AppException(_NOT_FOUND)
    updated = await repo.update(project, payload)
    return ProjectRead.model_validate(updated)


@router.delete("/{project_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: int,
    tenant: TenantContext = Depends(require_org_manager),
):
    repo = ProjectRepository(tenant.db, tenant.organization_id)
    project = await repo.get_by_id(project_id)
    if project is None:
        raise AppException(_NOT_FOUND)
    await repo.delete(project)
    return None


@router.get("/{project_id}/items")
async def get_project_items(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    if tenant.org_role == CLIENT:
        raise AppException(_CLIENT_FORBIDDEN)

    repo = ProjectRepository(db, tenant.organization_id)
    project = await repo.get_by_id(project_id)
    if project is None:
        raise AppException(_NOT_FOUND)
    await _require_project_access(tenant, repo, project_id)

    org_id = tenant.organization_id

    rocks_result = await db.execute(
        select(Rock)
        .where(Rock.project_id == project_id, Rock.organization_id == org_id)
        .order_by(Rock.created_at.desc())
    )
    rocks = rocks_result.scalars().all()

    kpis_result = await db.execute(
        select(KPI)
        .where(KPI.project_id == project_id, KPI.organization_id == org_id)
        .order_by(KPI.created_at.desc())
    )
    kpis = kpis_result.scalars().all()

    issues_result = await db.execute(
        select(Issue)
        .where(Issue.project_id == project_id, Issue.organization_id == org_id)
        .order_by(Issue.priority.desc(), Issue.created_at.desc())
    )
    issues = issues_result.scalars().all()

    # ── Objectives of this project, plus the Rocks under them and the KPIs
    #    tracking those Rocks (additive keys — existing keys are unchanged). ──
    objectives_result = await db.execute(
        select(Objective)
        .where(Objective.project_id == project_id, Objective.organization_id == org_id)
        .options(selectinload(Objective.owner), selectinload(Objective.project))
        .order_by(Objective.created_at.desc())
    )
    objectives = objectives_result.scalars().all()

    objective_ids = [o.id for o in objectives]
    objective_rocks = []
    if objective_ids:
        objective_rocks_result = await db.execute(
            select(Rock)
            .where(Rock.objective_id.in_(objective_ids), Rock.organization_id == org_id)
            .order_by(Rock.created_at.desc())
        )
        objective_rocks = objective_rocks_result.scalars().all()

    # KPIs tracking any rock shown on this page (project rocks + objective rocks).
    all_rock_ids = {r.id for r in rocks} | {r.id for r in objective_rocks}
    rock_kpis = []
    if all_rock_ids:
        rock_kpis_result = await db.execute(
            select(KPI)
            .where(KPI.rock_id.in_(all_rock_ids), KPI.organization_id == org_id)
            .order_by(KPI.created_at.desc())
        )
        rock_kpis = rock_kpis_result.scalars().all()

    # Teams involved in this project: via its rocks, KPIs, and tasks.
    tasks_result = await db.execute(
        select(Task.team_id).where(Task.project_id == project_id, Task.organization_id == org_id)
    )
    team_ids = (
        {r.team_id for r in rocks}
        | {r.team_id for r in objective_rocks}
        | {k.team_id for k in kpis}
        | {k.team_id for k in rock_kpis}
        | {row[0] for row in tasks_result.all() if row[0] is not None}
    )
    team_repo = TeamRepository(db, org_id)
    project_teams = [t for t in await team_repo.list_all() if t.id in team_ids]

    return {
        "rocks": [RockOut.model_validate(r) for r in rocks],
        "kpis": [KPIOut.model_validate(k) for k in kpis],
        "issues": [IssueOut.model_validate(i) for i in issues],
        "objectives": [ObjectiveRead.model_validate(o) for o in objectives],
        "objective_rocks": [RockOut.model_validate(r) for r in objective_rocks],
        "rock_kpis": [KPIOut.model_validate(k) for k in rock_kpis],
        "teams": [serialize_team(t) for t in project_teams],
    }


def _serialize_member(membership) -> ProjectMemberOut:
    user = membership.user
    return ProjectMemberOut(
        id=membership.id,
        user_id=membership.user_id,
        full_name=user.full_name if user else None,
        email=user.email if user else "",
    )


async def _pm_user_ids(tenant: TenantContext, user_ids: list[int]) -> set[int]:
    if not user_ids:
        return set()
    result = await tenant.db.execute(
        select(OrganizationMembership.user_id).where(
            OrganizationMembership.organization_id == tenant.organization_id,
            OrganizationMembership.user_id.in_(user_ids),
            OrganizationMembership.role == PROJECT_MANAGER,
        )
    )
    return {row[0] for row in result.all()}


@router.get("/{project_id}/members", response_model=list[ProjectMemberOut])
async def list_project_members(
    project_id: int,
    tenant: TenantContext = Depends(require_org_manager),
):
    """Project Manager assignments only. `ProjectMembership` is also used to
    grant Clients access to a project (see `project_invitations.py`), but
    this endpoint backs the staff-only "assign a Project Manager" UI, so
    Clients who accepted an invitation must never show up here."""
    repo = ProjectRepository(tenant.db, tenant.organization_id)
    if await repo.get_by_id(project_id) is None:
        raise AppException(_NOT_FOUND)

    members = await repo.list_members(project_id)
    if not members:
        return []

    pm_user_ids = await _pm_user_ids(tenant, [m.user_id for m in members])
    return [_serialize_member(m) for m in members if m.user_id in pm_user_ids]


@router.post("/{project_id}/members", response_model=ProjectMemberOut, status_code=http_status.HTTP_201_CREATED)
async def add_project_member(
    project_id: int,
    payload: ProjectMemberAssign,
    tenant: TenantContext = Depends(require_org_manager),
):
    """A project has exactly one Project Manager — assigning a new one
    replaces whichever Project Manager was previously assigned (Client
    memberships on this same project are left untouched)."""
    repo = ProjectRepository(tenant.db, tenant.organization_id)
    if await repo.get_by_id(project_id) is None:
        raise AppException(_NOT_FOUND)

    existing_members = await repo.list_members(project_id)
    other_user_ids = [m.user_id for m in existing_members if m.user_id != payload.user_id]
    pm_user_ids = await _pm_user_ids(tenant, other_user_ids)
    for uid in pm_user_ids:
        await repo.remove_member(project_id, uid)

    membership = await repo.add_member(project_id, payload.user_id)
    return _serialize_member(membership)


@router.delete("/{project_id}/members/{user_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def remove_project_member(
    project_id: int,
    user_id: int,
    tenant: TenantContext = Depends(require_org_manager),
):
    repo = ProjectRepository(tenant.db, tenant.organization_id)
    if await repo.get_by_id(project_id) is None:
        raise AppException(_NOT_FOUND)
    await repo.remove_member(project_id, user_id)
    return None
