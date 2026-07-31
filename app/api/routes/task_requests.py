from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi import status as http_status
from sqlalchemy import select

from app.core.auth_errors import AppException, ErrorDef
from app.core.org_roles import ADMIN, CLIENT, OWNER, PROJECT_MANAGER
from app.core.tenant import TenantContext, get_tenant_context
from app.models.notification import Notification
from app.models.organization import OrganizationMembership
from app.models.project import ProjectMembership
from app.models.user import User
from app.repositories.project_repository import ProjectRepository
from app.repositories.task_repository import TaskRepository
from app.repositories.task_request_repository import TaskRequestRepository
from app.schemas.task import TaskCreate
from app.schemas.task_request import (
    TaskRequestConvert,
    TaskRequestCreate,
    TaskRequestOut,
    TaskRequestReject,
)
from app.services.background_email import (
    bg_send_client_task_request,
    bg_send_task_assigned,
    bg_send_task_request_reviewed,
)

router = APIRouter(prefix="/projects/{project_id}/task-requests", tags=["Task Requests"])

_PROJECT_NOT_FOUND = ErrorDef(code="PROJECT_NOT_FOUND", status=http_status.HTTP_404_NOT_FOUND, message="Project not found.")
_NOT_ASSIGNED = ErrorDef(code="PROJECT_NOT_ASSIGNED", status=http_status.HTTP_403_FORBIDDEN, message="You are not assigned to this project.")
_STAFF_ONLY = ErrorDef(code="TASK_REQUEST_STAFF_ONLY", status=http_status.HTTP_403_FORBIDDEN, message="Only staff can perform this action.")
_REQUEST_NOT_FOUND = ErrorDef(code="TASK_REQUEST_NOT_FOUND", status=http_status.HTTP_404_NOT_FOUND, message="Task request not found.")
_ALREADY_REVIEWED = ErrorDef(code="TASK_REQUEST_ALREADY_REVIEWED", status=http_status.HTTP_409_CONFLICT, message="This task request has already been reviewed.")


async def _require_is_staff(tenant: TenantContext, project_repo: ProjectRepository, project_id: int) -> None:
    if tenant.is_manager_or_above:
        return
    if tenant.org_role == PROJECT_MANAGER and await project_repo.is_member(project_id, tenant.user.id):
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
    project_repo = ProjectRepository(tenant.db, tenant.organization_id)
    if await project_repo.get_by_id(project_id) is None:
        raise AppException(_PROJECT_NOT_FOUND)
    await _require_is_staff(tenant, project_repo, project_id)

    repo = TaskRequestRepository(tenant.db, tenant.organization_id)
    request = await repo.get_by_id(request_id)
    if request is None or request.project_id != project_id:
        raise AppException(_REQUEST_NOT_FOUND)
    if request.status != "pending":
        raise AppException(_ALREADY_REVIEWED)

    task_repo = TaskRepository(tenant.db, tenant.organization_id)
    task = await task_repo.create(
        TaskCreate(
            name=request.title,
            project_id=project_id,
            team_id=payload.team_id,
            assignee_id=payload.assignee_id,
            priority=payload.priority,
            due_date=payload.due_date,
        ),
        created_by_id=tenant.user.id,
    )

    request = await repo.mark_converted(request, task.id, tenant.user.id)

    if payload.assignee_id and payload.assignee_id != tenant.user.id:
        tenant.db.add(
            Notification(
                user_id=payload.assignee_id,
                task_id=task.id,
                project_id=project_id,
                title="New task assigned to you",
                message=f"You have been assigned a new task: '{task.name}'.",
                type="task_assigned",
            )
        )
        await tenant.db.commit()
        background_tasks.add_task(bg_send_task_assigned, task.id, payload.assignee_id, tenant.user.id)

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
    project_repo = ProjectRepository(tenant.db, tenant.organization_id)
    if await project_repo.get_by_id(project_id) is None:
        raise AppException(_PROJECT_NOT_FOUND)
    await _require_is_staff(tenant, project_repo, project_id)

    repo = TaskRequestRepository(tenant.db, tenant.organization_id)
    request = await repo.get_by_id(request_id)
    if request is None or request.project_id != project_id:
        raise AppException(_REQUEST_NOT_FOUND)
    if request.status != "pending":
        raise AppException(_ALREADY_REVIEWED)

    request = await repo.mark_rejected(request, tenant.user.id)

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
