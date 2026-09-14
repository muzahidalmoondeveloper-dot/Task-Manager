from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi import status as http_status
from sqlalchemy import select

from app.core.auth_errors import AppException, ErrorDef
from app.core.org_roles import ADMIN, CLIENT, OWNER, PROJECT_MANAGER
from app.core.project_access import list_project_team_ids
from app.core.tenant import TenantContext, get_tenant_context
from app.models.notification import Notification
from app.models.organization import OrganizationMembership
from app.models.project import ProjectMembership
from app.models.user import User
from app.repositories.project_repository import ProjectRepository
from app.repositories.task_repository import TaskRepository
from app.repositories.task_request_repository import TaskRequestRepository
from app.repositories.team_repository import TeamRepository
from app.schemas.task import TaskCreate
from app.schemas.task_request import (
    TaskRequestConvert,
    TaskRequestCreate,
    TaskRequestOut,
    TaskRequestReject,
)
from app.services.background_email import (
    bg_send_client_task_request,
    bg_send_task_request_reviewed,
)

router = APIRouter(prefix="/projects/{project_id}/task-requests", tags=["Task Requests"])

_PROJECT_NOT_FOUND = ErrorDef(code="PROJECT_NOT_FOUND", status=http_status.HTTP_404_NOT_FOUND, message="Project not found.")
_NOT_ASSIGNED = ErrorDef(code="PROJECT_NOT_ASSIGNED", status=http_status.HTTP_403_FORBIDDEN, message="You are not assigned to this project.")
_STAFF_ONLY = ErrorDef(code="TASK_REQUEST_STAFF_ONLY", status=http_status.HTTP_403_FORBIDDEN, message="Only staff can perform this action.")
_REQUEST_NOT_FOUND = ErrorDef(code="TASK_REQUEST_NOT_FOUND", status=http_status.HTTP_404_NOT_FOUND, message="Task request not found.")
_ALREADY_REVIEWED = ErrorDef(code="TASK_REQUEST_ALREADY_REVIEWED", status=http_status.HTTP_409_CONFLICT, message="This task request has already been reviewed.")
# Client Task Request conversion follow-up: "team is not assignable
# within this project" now applies to every converting role (not only a
# plain Project Manager) — see convert_task_request. A Team must be
# explicitly attached to the request's Project through the existing
# Project<->Team association (app.core.project_access.list_project_team_ids)
# regardless of who is converting; this also rejects a cross-tenant team
# id, since that association can never include one.
_TEAM_NOT_ASSIGNABLE = ErrorDef(code="TEAM_NOT_ASSIGNABLE", status=http_status.HTTP_403_FORBIDDEN, message="That team is not assignable within this project.")


async def _require_is_staff(tenant: TenantContext, project_repo: ProjectRepository, project_id: int) -> None:
    """Owner/Admin can review/manage a client task request on any project.
    A Team Manager or a Project Manager (role or granted flag) may only do
    so for a project they're explicitly assigned to (ProjectMembership) —
    matches app.core.project_access's rule that Team Manager has no
    organization-wide project access. Previously `is_manager_or_above`
    alone (true for Team Manager) returned early with no project check,
    letting a plain Team Manager manage client task requests on ANY
    project in the org."""
    if tenant.is_admin_or_owner:
        return
    if (tenant.is_manager_or_above or tenant.has_project_manager_access) and await project_repo.is_member(project_id, tenant.user.id):
        return
    raise AppException(_STAFF_ONLY)


async def _notify_recipients(tenant: TenantContext, project_id: int) -> list[User]:
    """Project Managers assigned to this project, plus every Owner/Admin in the
    org — leadership always sees client task requests regardless of whether a
    PM is assigned."""
    pm_result = await tenant.db.execute(
        select(User)
        .join(OrganizationMembership, OrganizationMembership.user_id == User.id)
        .join(ProjectMembership, ProjectMembership.user_id == User.id)
        .where(
            OrganizationMembership.organization_id == tenant.organization_id,
            (OrganizationMembership.role == PROJECT_MANAGER) | (OrganizationMembership.is_project_manager.is_(True)),
            ProjectMembership.project_id == project_id,
        )
    )
    management_result = await tenant.db.execute(
        select(User)
        .join(OrganizationMembership, OrganizationMembership.user_id == User.id)
        .where(
            OrganizationMembership.organization_id == tenant.organization_id,
            OrganizationMembership.role.in_({OWNER, ADMIN}),
        )
    )

    recipients = {}
    for user in [*pm_result.scalars().all(), *management_result.scalars().all()]:
        recipients[user.id] = user
    return list(recipients.values())


@router.post("", response_model=TaskRequestOut, status_code=http_status.HTTP_201_CREATED)
async def create_task_request(
    project_id: int,
    payload: TaskRequestCreate,
    background_tasks: BackgroundTasks,
    tenant: TenantContext = Depends(get_tenant_context),
):
    if tenant.org_role != CLIENT:
        raise AppException(_STAFF_ONLY, message="Only clients can submit task requests.")

    project_repo = ProjectRepository(tenant.db, tenant.organization_id)
    if await project_repo.get_by_id(project_id) is None:
        raise AppException(_PROJECT_NOT_FOUND)
    if not await project_repo.is_member(project_id, tenant.user.id):
        raise AppException(_NOT_ASSIGNED)

    repo = TaskRequestRepository(tenant.db, tenant.organization_id)
    request = await repo.create(project_id, tenant.user.id, payload)

    recipients = await _notify_recipients(tenant, project_id)
    for recipient in recipients:
        tenant.db.add(
            Notification(
                user_id=recipient.id,
                project_id=project_id,
                title="New task request",
                message=f"{tenant.user.full_name} submitted a task request: '{request.title}'.",
                type="task_request_submitted",
            )
        )
    await tenant.db.commit()

    for recipient in recipients:
        background_tasks.add_task(bg_send_client_task_request, request.id, recipient.id, tenant.user.id)

    return TaskRequestOut.model_validate(request)


@router.get("", response_model=list[TaskRequestOut])
async def list_task_requests(
    project_id: int,
    tenant: TenantContext = Depends(get_tenant_context),
):
    project_repo = ProjectRepository(tenant.db, tenant.organization_id)
    if await project_repo.get_by_id(project_id) is None:
        raise AppException(_PROJECT_NOT_FOUND)

    if tenant.org_role == CLIENT:
        if not await project_repo.is_member(project_id, tenant.user.id):
            raise AppException(_NOT_ASSIGNED)
        repo = TaskRequestRepository(tenant.db, tenant.organization_id)
        requests = await repo.list_for_project(project_id)
        return [TaskRequestOut.model_validate(r) for r in requests if r.submitted_by_id == tenant.user.id]

    await _require_is_staff(tenant, project_repo, project_id)
    repo = TaskRequestRepository(tenant.db, tenant.organization_id)
    return [TaskRequestOut.model_validate(r) for r in await repo.list_for_project(project_id)]


@router.post("/{request_id}/convert", response_model=TaskRequestOut)
async def convert_task_request(
    project_id: int,
    request_id: int,
    payload: TaskRequestConvert,
    background_tasks: BackgroundTasks,
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Client Task Request -> Task conversion follow-up.

    Root cause of the original "PM cannot convert" bug: `TaskRequestConvert`
    required a `team_id` on every conversion (no way to omit it), and the
    frontend still offered an optional individual `assignee_id` dropdown —
    but the backend's plain-PM branch rejected any non-null `assignee_id`
    outright. There was no "take it for myself" path at all: a plain PM
    picking a team member (the only individual-owner UI available) always
    hit `_PM_CANNOT_ASSIGN_MEMBER` and had no other way to complete a
    conversion. Fixed by replacing the implicit team_id/assignee_id shape
    with the explicit `conversion_mode` contract below, which has exactly
    two legal shapes and never exposes an individual assignee.

    Atomicity/double-conversion: the request row is locked with
    SELECT ... FOR UPDATE (get_by_id_for_update) and the new Task's insert
    plus the request's status flip share a single commit — see that
    method's and TaskRepository.create_no_commit's docstrings. If Task
    creation raises (e.g. a failed assignee/team validation), nothing is
    committed and the request stays "pending" for a retry.
    """
    project_repo = ProjectRepository(tenant.db, tenant.organization_id)
    if await project_repo.get_by_id(project_id) is None:
        raise AppException(_PROJECT_NOT_FOUND)
    await _require_is_staff(tenant, project_repo, project_id)

    repo = TaskRequestRepository(tenant.db, tenant.organization_id)
    request = await repo.get_by_id_for_update(request_id)
    if request is None or request.project_id != project_id:
        raise AppException(_REQUEST_NOT_FOUND)
    if request.status != "pending":
        raise AppException(_ALREADY_REVIEWED)

    team_id: int | None = None
    assignee_id: int | None = None

    if payload.conversion_mode == "team":
        team_id = payload.team_id
        # Defense-in-depth: confirm the team actually exists IN THIS ORG
        # first (TeamRepository is tenant-scoped — same explicit existence
        # check app.api.routes.tasks.create_task performs before its own
        # project-attachment check), rejecting a cross-tenant or
        # nonexistent id with a plain 403 rather than leaking whether an
        # id merely isn't attached vs. doesn't exist at all.
        team_repo = TeamRepository(tenant.db, tenant.organization_id)
        if await team_repo.get_by_id(team_id) is None:
            raise AppException(_TEAM_NOT_ASSIGNABLE)
        # Project<->Team validation applies to EVERY converting role, not
        # only a plain Project Manager — the selected Team must be
        # explicitly attached to this exact Project through the existing
        # association (app.core.project_access.list_project_team_ids),
        # never an arbitrary org team or a non-attached team injected
        # through direct API manipulation.
        allowed_team_ids = await list_project_team_ids(tenant.db, tenant.organization_id, project_id)
        if team_id not in allowed_team_ids:
            raise AppException(_TEAM_NOT_ASSIGNABLE)
        # assignee_id stays None — the Team Manager decides the individual
        # owner later; a PM (or anyone converting) never picks one here.
    else:
        # "self": the AUTHENTICATED converting user becomes the assignee,
        # derived server-side. There is no assignee_id field on this
        # schema at all — a forged assignee cannot be smuggled in.
        assignee_id = tenant.user.id

    task_repo = TaskRepository(tenant.db, tenant.organization_id)
    task = await task_repo.create_no_commit(
        TaskCreate(
            name=request.title,
            description=request.description,
            project_id=project_id,
            team_id=team_id,
            assignee_id=assignee_id,
            priority=payload.priority,
            due_date=payload.due_date,
        ),
        created_by_id=tenant.user.id,
    )
    repo.mark_converted_no_commit(request, task.id, tenant.user.id)
    await tenant.db.commit()
    request = await repo.get_by_id(request.id)

    if request.submitted_by_id:
        tenant.db.add(
            Notification(
                user_id=request.submitted_by_id,
                project_id=project_id,
                title="Task request approved",
                message=f"Your task request '{request.title}' was approved and converted into a task.",
                type="task_request_approved",
            )
        )
        await tenant.db.commit()
        background_tasks.add_task(bg_send_task_request_reviewed, request.id, request.submitted_by_id, tenant.user.id, True)

    return TaskRequestOut.model_validate(request)


@router.post("/{request_id}/reject", response_model=TaskRequestOut)
async def reject_task_request(
    project_id: int,
    request_id: int,
    payload: TaskRequestReject,
    background_tasks: BackgroundTasks,
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Reject/Convert race-condition follow-up: this route previously read
    the request with a plain (unlocked) get_by_id() while
    convert_task_request already locked it with SELECT ... FOR UPDATE — so
    a Convert and a Reject firing at the same moment could both observe
    status="pending" before either committed, and both proceed: a Task
    gets created AND the request ends up "rejected", an inconsistent final
    state. Fixed by locking the row here too (get_by_id_for_update), the
    exact same TOCTOU pattern convert_task_request already uses (see that
    method's docstring) — whichever of the two commits first wins the
    lock, and the other re-reads the now-committed status and is rejected
    by the unchanged `if request.status != "pending"` check below."""
    project_repo = ProjectRepository(tenant.db, tenant.organization_id)
    if await project_repo.get_by_id(project_id) is None:
        raise AppException(_PROJECT_NOT_FOUND)
    await _require_is_staff(tenant, project_repo, project_id)

    repo = TaskRequestRepository(tenant.db, tenant.organization_id)
    request = await repo.get_by_id_for_update(request_id)
    if request is None or request.project_id != project_id:
        raise AppException(_REQUEST_NOT_FOUND)
    if request.status != "pending":
        raise AppException(_ALREADY_REVIEWED)

    repo.mark_rejected_no_commit(request, tenant.user.id)
    await tenant.db.commit()
    request = await repo.get_by_id(request.id)

    if request.submitted_by_id:
        reason_suffix = f" Reason: {payload.reason}" if payload.reason else ""
        tenant.db.add(
            Notification(
                user_id=request.submitted_by_id,
                project_id=project_id,
                title="Task request declined",
                message=f"Your task request '{request.title}' was declined.{reason_suffix}",
                type="task_request_rejected",
            )
        )
        await tenant.db.commit()
        background_tasks.add_task(bg_send_task_request_reviewed, request.id, request.submitted_by_id, tenant.user.id, False)

    return TaskRequestOut.model_validate(request)
