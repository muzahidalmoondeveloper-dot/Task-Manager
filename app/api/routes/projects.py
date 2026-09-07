from datetime import datetime, timezone

from fastapi import APIRouter, Depends, UploadFile
from fastapi import File as FastAPIFile
from fastapi import status as http_status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.routes.teams import _serialize as serialize_team
from app.core.activity_actions import (
    ENTITY_PROJECT,
    PROJECT_CREATED,
    PROJECT_DELETED,
    PROJECT_MANAGER_ASSIGNED,
    PROJECT_MANAGER_REMOVED,
    PROJECT_UPDATED,
)
from app.core.auth_errors import AppException, ErrorDef
from app.core.database import get_db
from app.core.org_roles import CLIENT, PROJECT_MANAGER
from app.core.project_access import (
    is_project_management_blocked,
    is_project_scoped,
    require_project_management_access,
)
from app.core.tenant import TenantContext, check_active_billing, get_tenant_context, require_org_admin, require_org_manager
from app.models.issue import Issue
from app.models.kpi import KPI
from app.models.objective import Objective
from app.models.organization import OrganizationMembership
from app.models.rock import Rock
from app.models.task import Task
from app.repositories.project_repository import ProjectRepository
from app.repositories.task_time_entry_repository import TaskTimeEntryRepository
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
from app.schemas.task_time_entry import ProjectTimeSummary
from app.services import activity_service, logo_upload_service

router = APIRouter(prefix="/projects", tags=["Projects"])

_NOT_FOUND = ErrorDef(code="PROJECT_NOT_FOUND", status=http_status.HTTP_404_NOT_FOUND, message="Project not found.")
_PLAN_LIMIT = ErrorDef(code="PLAN_LIMIT_EXCEEDED", status=http_status.HTTP_402_PAYMENT_REQUIRED, message="Your plan's project limit has been reached.")
_CLIENT_FORBIDDEN = ErrorDef(code="CLIENT_ITEMS_FORBIDDEN", status=http_status.HTTP_403_FORBIDDEN, message="Clients view project progress through Reports, not this endpoint.")

_LOGO_DIR_NAME = "project-logos"

# app.core.project_access is the single shared source every route touching
# project-scoped data (this file, tasks.py, ...) consults, so the rule
# can't drift or be forgotten per-route. Kept as module-level aliases here
# so this file's many existing call sites don't all need renaming.
#
# This file specifically uses the *Projects-management-system* variants
# (require_project_management_access, not the plainer require_project_access
# tasks.py still uses) — a plain Team Manager must be blocked outright from
# every route below, never merely membership-scoped, even though a
# ProjectMembership row may exist for them (see app.core.project_access's
# module docstring for the full rule).
_is_project_scoped = is_project_scoped
_require_project_access = require_project_management_access


def _serialize_project(project, managers: dict[int, tuple[int, str | None]]) -> ProjectRead:
    read = ProjectRead.model_validate(project)
    manager = managers.get(project.id)
    if manager is not None:
        read.project_manager_id, read.project_manager_name = manager
    return read


@router.get("", response_model=list[ProjectRead])
async def list_projects(tenant: TenantContext = Depends(get_tenant_context)):
    repo = ProjectRepository(tenant.db, tenant.organization_id)
    # A plain Team Manager (no Project Manager/Admin capability) sees no
    # projects here at all — not even ones they hold a ProjectMembership
    # row on (see app.core.project_access.is_project_management_blocked).
    # Everyone else keeps the existing rule: membership-scoped for
    # Project Manager/Client, unrestricted for Owner/Admin/Team Member.
    if is_project_management_blocked(tenant):
        projects = []
    else:
        projects = (
            await repo.list_for_user(tenant.user.id) if _is_project_scoped(tenant) else await repo.list_all()
        )
    # One bulk query for every project's assigned manager (see
    # get_project_managers()'s docstring) instead of N+1 — so the Projects
    # list can show which Project Manager owns each project at a glance.
    managers = await repo.get_project_managers([p.id for p in projects])
    return [_serialize_project(p, managers) for p in projects]


@router.post("", response_model=ProjectRead, status_code=http_status.HTTP_201_CREATED)
async def create_project(
    payload: ProjectCreate,
    # Creating a brand-new Project is organization-wide by nature (there's
    # no existing project to scope the check against yet) — Owner/Admin
    # only. Previously require_org_manager (Owner/Admin/Team Manager),
    # which let a plain Team Manager create arbitrary org-wide projects; a
    # Team Manager is a team-scoped role and has no project-management
    # authority by design (see app.core.project_access).
    tenant: TenantContext = Depends(require_org_admin),
):
    check_active_billing(tenant)
    limits = tenant.plan_limits
    if limits.max_projects != -1:
        repo_check = ProjectRepository(tenant.db, tenant.organization_id)
        projects = await repo_check.list_all()
        if len(projects) >= limits.max_projects:
            raise AppException(_PLAN_LIMIT, details={"limit": limits.max_projects})

    repo = ProjectRepository(tenant.db, tenant.organization_id)
    project = await repo.create(payload, created_by_id=tenant.user.id)
    await activity_service.record(
        tenant.db, organization_id=tenant.organization_id, actor=tenant.user,
        action=PROJECT_CREATED, entity_type=ENTITY_PROJECT, entity_id=project.id, entity_label=project.name,
    )
    return ProjectRead.model_validate(project)


@router.get("/{project_id}", response_model=ProjectRead)
async def get_project(project_id: int, tenant: TenantContext = Depends(get_tenant_context)):
    repo = ProjectRepository(tenant.db, tenant.organization_id)
    project = await repo.get_by_id(project_id)
    if project is None:
        raise AppException(_NOT_FOUND)
    await _require_project_access(tenant, repo, project_id)
    managers = await repo.get_project_managers([project_id])
    return _serialize_project(project, managers)


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
    await _require_project_access(tenant, repo, project_id)
    before_name, before_status = project.name, project.status
    updated = await repo.update(project, payload)

    fields_changed = []
    metadata: dict = {}
    if updated.name != before_name:
        fields_changed.append("name")
    if updated.status != before_status:
        metadata["status_from"], metadata["status_to"] = before_status, updated.status
        fields_changed.append("status")
    if fields_changed:
        metadata["fields_changed"] = fields_changed
        await activity_service.record(
            tenant.db, organization_id=tenant.organization_id, actor=tenant.user,
            action=PROJECT_UPDATED, entity_type=ENTITY_PROJECT, entity_id=updated.id, entity_label=updated.name,
            metadata=metadata,
        )
    return ProjectRead.model_validate(updated)


@router.post("/{project_id}/logo", response_model=ProjectRead)
async def upload_project_logo(
    project_id: int,
    tenant: TenantContext = Depends(require_org_manager),
    file: UploadFile = FastAPIFile(...),
):
    repo = ProjectRepository(tenant.db, tenant.organization_id)
    project = await repo.get_by_id(project_id)
    if project is None:
        raise AppException(_NOT_FOUND)
    await _require_project_access(tenant, repo, project_id)

    new_url = await logo_upload_service.save_logo(file, _LOGO_DIR_NAME, project_id)

    # Save-then-commit-then-delete-old (see the org logo / avatar upload
    # routes for the same ordering rationale).
    previous_url = project.logo_url
    try:
        project.logo_url = new_url
        await tenant.db.commit()
    except Exception:
        await tenant.db.rollback()
        await logo_upload_service.delete_logo_file(new_url, _LOGO_DIR_NAME)
        raise

    await logo_upload_service.delete_logo_file(previous_url, _LOGO_DIR_NAME)
    await tenant.db.refresh(project)
    return ProjectRead.model_validate(project)


@router.delete("/{project_id}/logo", response_model=ProjectRead)
async def delete_project_logo(
    project_id: int,
    tenant: TenantContext = Depends(require_org_manager),
):
    repo = ProjectRepository(tenant.db, tenant.organization_id)
    project = await repo.get_by_id(project_id)
    if project is None:
        raise AppException(_NOT_FOUND)
    await _require_project_access(tenant, repo, project_id)

    previous_url = project.logo_url
    project.logo_url = None
    await tenant.db.commit()
    await tenant.db.refresh(project)

    await logo_upload_service.delete_logo_file(previous_url, _LOGO_DIR_NAME)
    return ProjectRead.model_validate(project)


@router.delete("/{project_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: int,
    tenant: TenantContext = Depends(require_org_manager),
):
    repo = ProjectRepository(tenant.db, tenant.organization_id)
    project = await repo.get_by_id(project_id)
    if project is None:
        raise AppException(_NOT_FOUND)
    await _require_project_access(tenant, repo, project_id)
    project_id_value, project_name = project.id, project.name
    await repo.delete(project)
    await activity_service.record(
        tenant.db, organization_id=tenant.organization_id, actor=tenant.user,
        action=PROJECT_DELETED, entity_type=ENTITY_PROJECT, entity_id=project_id_value, entity_label=project_name,
    )
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
        "teams": [await serialize_team(db, org_id, t) for t in project_teams],
    }


@router.get("/{project_id}/working-time", response_model=ProjectTimeSummary)
async def get_project_working_time(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Project Working Time (#7B) — a dedicated, lightweight endpoint
    (rather than folding this into GET /{project_id}/items) so the
    frontend can refresh just this number after a task timer Start/Stop,
    or periodically while a timer is running, without re-fetching that
    endpoint's much heavier rocks/KPIs/issues/objectives/teams payload.

    Uses the exact same authorization as GET /{project_id}/items and every
    other Project Detail route (`_require_project_access` ==
    require_project_management_access) — Owner/Admin unrestricted, a
    genuine Project Manager membership-scoped, a plain Team Manager
    rejected outright regardless of any ProjectMembership row. Nothing
    broader is used merely because this is "just a metric."
    """
    if tenant.org_role == CLIENT:
        raise AppException(_CLIENT_FORBIDDEN)

    repo = ProjectRepository(db, tenant.organization_id)
    project = await repo.get_by_id(project_id)
    if project is None:
        raise AppException(_NOT_FOUND)
    await _require_project_access(tenant, repo, project_id)

    now = datetime.now(timezone.utc)
    time_repo = TaskTimeEntryRepository(db, tenant.organization_id)
    working_time_seconds, active_timer_count = await time_repo.get_project_time_summary(project_id, now)

    return ProjectTimeSummary(
        project_id=project_id,
        working_time_seconds=working_time_seconds,
        active_timer_count=active_timer_count,
        calculated_at=now,
    )


def _serialize_member(membership) -> ProjectMemberOut:
    user = membership.user
    return ProjectMemberOut(
        id=membership.id,
        user_id=membership.user_id,
        full_name=user.full_name if user else None,
        email=user.email if user else "",
    )


async def _pm_user_ids(tenant: TenantContext, user_ids: list[int]) -> set[int]:
    """Users who count as 'the Project Manager' for assignment purposes:
    those with the functional project_manager role, plus anyone (e.g. a team
    manager) additionally granted project-manager privileges."""
    if not user_ids:
        return set()
    result = await tenant.db.execute(
        select(OrganizationMembership.user_id).where(
            OrganizationMembership.organization_id == tenant.organization_id,
            OrganizationMembership.user_id.in_(user_ids),
            (OrganizationMembership.role == PROJECT_MANAGER) | (OrganizationMembership.is_project_manager.is_(True)),
        )
    )
    return {row[0] for row in result.all()}


@router.get("/{project_id}/members", response_model=list[ProjectMemberOut])
async def list_project_members(
    project_id: int,
    # Backs the staff-only "assign a Project Manager to this project" admin
    # screen (see docstring below) — an organization-wide project-management
    # action, not a per-project one, so this had no _require_project_access
    # scoping to layer on top of a role check in the first place. Owner/
    # Admin only. Previously require_org_manager (Owner/Admin/Team Manager),
    # which let a plain Team Manager view/assign/remove the Project Manager
    # of ANY project in the org — a team-scoped role has no business
    # reassigning who manages an arbitrary project.
    tenant: TenantContext = Depends(require_org_admin),
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
    tenant: TenantContext = Depends(require_org_admin),
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
    project = await repo.get_by_id(project_id)
    await activity_service.record(
        tenant.db, organization_id=tenant.organization_id, actor=tenant.user,
        action=PROJECT_MANAGER_ASSIGNED, entity_type=ENTITY_PROJECT, entity_id=project_id,
        entity_label=project.name if project else None, metadata={"user_id": payload.user_id},
    )
    return _serialize_member(membership)


@router.delete("/{project_id}/members/{user_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def remove_project_member(
    project_id: int,
    user_id: int,
    tenant: TenantContext = Depends(require_org_admin),
):
    repo = ProjectRepository(tenant.db, tenant.organization_id)
    project = await repo.get_by_id(project_id)
    if project is None:
        raise AppException(_NOT_FOUND)
    await repo.remove_member(project_id, user_id)
    await activity_service.record(
        tenant.db, organization_id=tenant.organization_id, actor=tenant.user,
        action=PROJECT_MANAGER_REMOVED, entity_type=ENTITY_PROJECT, entity_id=project_id,
        entity_label=project.name, metadata={"user_id": user_id},
    )
    return None
