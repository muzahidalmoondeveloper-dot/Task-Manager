import logging
from datetime import date, datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi import status as http_status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_errors import AppException, ErrorDef
from app.core.database import get_db
from app.core.org_roles import PROJECT_MANAGER, TEAM_MEMBER
from app.core.project_access import is_project_scoped, list_project_team_ids, require_project_access
from app.core.tenant import TenantContext, get_tenant_context, require_org_admin, require_org_manager
from app.models.notification import Notification
from app.models.task import Task
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.repositories.project_repository import ProjectRepository
from app.repositories.task_repository import TaskRepository
from app.repositories.team_repository import TeamRepository
from app.repositories.user_repository import UserRepository
from app.schemas.project import ProjectRead
from app.schemas.task import (
    AssignBackRequest,
    TaskCreate,
    TaskDetailRead,
    TaskStatusUpdate,
    TaskUpdate,
)
from app.schemas.team import TeamRead
from app.schemas.user import UserRead
from app.services.background_email import (
    bg_send_due_date_updated,
    bg_send_task_approved,
    bg_send_task_assigned,
    bg_send_task_assigned_back,
    bg_send_task_sent_for_review,
)

router = APIRouter(prefix="/tasks", tags=["Tasks"])
logger = logging.getLogger("tasks")

_TASK_NOT_FOUND = ErrorDef(code="TASK_NOT_FOUND", status=http_status.HTTP_404_NOT_FOUND, message="Task not found.")


def serialize_task(task: Task) -> TaskDetailRead:
    return TaskDetailRead(
        id=task.id,
        name=task.name,
        icon=getattr(task, "icon", None),
        start_date=task.start_date,
        due_date=task.due_date,
        status=task.status,
        priority=getattr(task, "priority", "medium"),
        assignee_id=task.assignee_id,
        project_id=task.project_id,
        team_id=task.team_id,
        created_by_id=task.created_by_id,
        completed_by_id=task.completed_by_id,
        completed_at=task.completed_at,
        reviewed_by_id=task.reviewed_by_id,
        reviewed_at=task.reviewed_at,
        review_note=task.review_note,
        assignee=UserRead.model_validate(task.assignee) if task.assignee else None,
        project=ProjectRead.model_validate(task.project) if task.project else None,
        team=TeamRead.model_validate(task.team) if task.team else None,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


async def create_notification(*, db, user_id, task_id, title, message, type_):
    db.add(Notification(user_id=user_id, task_id=task_id, title=title, message=message, type=type_))


async def get_task_or_404(tenant: TenantContext, task_id: int) -> Task:
    repo = TaskRepository(tenant.db, tenant.organization_id)
    task = await repo.get_by_id(task_id)
    if task is None:
        raise AppException(_TASK_NOT_FOUND)
    return task


# ── My tasks (all members) ────────────────────────────────────────────────────

@router.get("/my", response_model=list[TaskDetailRead])
async def list_my_tasks(
    status_filter: str | None = Query(default=None, alias="status"),
    priority_filter: str | None = Query(default=None, alias="priority"),
    due_date_from: date | None = Query(default=None),
    due_date_to: date | None = Query(default=None),
    overdue: bool = Query(default=False),
    project_id: int | None = Query(default=None),
    team_id: int | None = Query(default=None),
    tenant: TenantContext = Depends(get_tenant_context),
):
    repo = TaskRepository(tenant.db, tenant.organization_id)
    tasks = await repo.list_for_assignee(
        tenant.user.id,
        status=status_filter, priority=priority_filter,
        project_id=project_id, team_id=team_id,
        due_date_from=due_date_from, due_date_to=due_date_to, overdue=overdue,
    )
    return [serialize_task(t) for t in tasks]


# ── All tasks (admin/manager) ─────────────────────────────────────────────────

@router.get("", response_model=list[TaskDetailRead])
async def list_tasks(
    status_filter: str | None = Query(default=None, alias="status"),
    priority_filter: str | None = Query(default=None, alias="priority"),
    assignee_id: int | None = Query(default=None),
    project_id: int | None = Query(default=None),
    team_id: int | None = Query(default=None),
    due_date_from: date | None = Query(default=None),
    due_date_to: date | None = Query(default=None),
    overdue: bool = Query(default=False),
    tenant: TenantContext = Depends(require_org_manager),
):
    repo = TaskRepository(tenant.db, tenant.organization_id)
    tasks = await repo.list_all(
        status=status_filter, priority=priority_filter,
        project_id=project_id, team_id=team_id, assignee_id=assignee_id,
        due_date_from=due_date_from, due_date_to=due_date_to, overdue=overdue,
    )
    return [serialize_task(t) for t in tasks]


# ── Create task ───────────────────────────────────────────────────────────────

@router.post("", response_model=TaskDetailRead, status_code=http_status.HTTP_201_CREATED)
async def create_task(
    payload: TaskCreate,
    background_tasks: BackgroundTasks,
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db),
):
    user_repo = UserRepository(db)
    project_repo = ProjectRepository(db, tenant.organization_id)
    team_repo = TeamRepository(db, tenant.organization_id)
    task_repo = TaskRepository(db, tenant.organization_id)

    if not tenant.is_manager_or_above and tenant.org_role != PROJECT_MANAGER:
        raise HTTPException(status_code=http_status.HTTP_403_FORBIDDEN, detail="You are not allowed to create tasks.")

    # Project scope (see app.core.project_access): a Team Manager or
    # Project Manager without ProjectMembership on the target project may
    # not create tasks under it — previously only plain PROJECT_MANAGER was
    # checked here, so a Team Manager (who passes is_manager_or_above and
    # skipped this block entirely) could create a task under ANY project in
    # the org, not just one they're assigned to manage.
    if payload.project_id is not None and is_project_scoped(tenant):
        if not await project_repo.is_member(payload.project_id, tenant.user.id):
            raise HTTPException(status_code=http_status.HTTP_403_FORBIDDEN, detail="You can only create tasks under a project you are assigned to.")

    if payload.assignee_id is not None:
        if await user_repo.get_by_id(payload.assignee_id) is None:
            raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail="Selected assignee is invalid.")

    if payload.project_id is not None:
        if await project_repo.get_by_id(payload.project_id) is None:
            raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail="Selected project is invalid.")

    if payload.team_id is not None:
        if await team_repo.get_by_id(payload.team_id) is None:
            raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail="Selected team is invalid.")

        # A plain Project Manager (not also Owner/Admin/Team Manager) may
        # only assign a team that's actually associated with the project
        # they're creating this task under — never an arbitrary org team,
        # even one that exists and passed the check just above. This is
        # the write-side enforcement of the same rule the "Assign Team"
        # dropdown now follows (GET /projects/{id}/items's derived team
        # list) — the frontend filtering it to the right options is a UX
        # nicety, not the security boundary; a crafted request must be
        # rejected the same way. Owners/Admins/Team Managers are unaffected
        # (unchanged, existing behavior).
        is_plain_project_manager = tenant.has_project_manager_access and not tenant.is_manager_or_above
        if is_plain_project_manager and payload.project_id is not None:
            allowed_team_ids = await list_project_team_ids(db, tenant.organization_id, payload.project_id)
            if payload.team_id not in allowed_team_ids:
                raise HTTPException(
                    status_code=http_status.HTTP_403_FORBIDDEN,
                    detail="That team is not assignable within this project.",
                )

    task = await task_repo.create(payload, created_by_id=tenant.user.id)

    if payload.assignee_id and payload.assignee_id != tenant.user.id:
        await create_notification(db=db, user_id=payload.assignee_id, task_id=task.id, title="New task assigned to you", message=f"You have been assigned a new task: '{task.name}'.", type_="task_assigned")
        await db.commit()
        assignee_user = await user_repo.get_by_id(payload.assignee_id)
        if assignee_user:
            background_tasks.add_task(bg_send_task_assigned, task.id, assignee_user.id, tenant.user.id)

    return serialize_task(task)


# ── List by project / team ────────────────────────────────────────────────────

@router.get("/project/{project_id}", response_model=list[TaskDetailRead])
async def list_tasks_by_project(
    project_id: int,
    tenant: TenantContext = Depends(get_tenant_context),
):
    project_repo = ProjectRepository(tenant.db, tenant.organization_id)
    task_repo = TaskRepository(tenant.db, tenant.organization_id)
    if await project_repo.get_by_id(project_id) is None:
        raise AppException(ErrorDef(code="PROJECT_NOT_FOUND", status=http_status.HTTP_404_NOT_FOUND, message="Project not found."))
    # A Project Manager (or Client) not assigned to this project gets a
    # hard 403 here — same rule every other project-scoped route enforces
    # (see app.core.project_access) — instead of silently falling through
    # to the "only your own assigned tasks" filter below, which used to let
    # them successfully call this endpoint for a project they don't manage
    # at all and get back a (non-error, if they happened to have any tasks
    # there) response.
    is_scoped = is_project_scoped(tenant)
    if is_scoped:
        await require_project_access(tenant, project_repo, project_id)

    tasks = await task_repo.list_by_project(project_id)
    # Full visibility for admins/owners, and for a Project Manager who IS
    # assigned here — they manage the whole project, not just their own
    # tasks within it.
    if tenant.is_admin_or_owner or is_scoped:
        return [serialize_task(t) for t in tasks]
    return [serialize_task(t) for t in tasks if t.assignee_id == tenant.user.id]


@router.get("/team/{team_id}", response_model=list[TaskDetailRead])
async def list_tasks_by_team(
    team_id: int,
    tenant: TenantContext = Depends(get_tenant_context),
):
    team_repo = TeamRepository(tenant.db, tenant.organization_id)
    task_repo = TaskRepository(tenant.db, tenant.organization_id)
    if await team_repo.get_by_id(team_id) is None:
        raise AppException(ErrorDef(code="TEAM_NOT_FOUND", status=http_status.HTTP_404_NOT_FOUND, message="Team not found."))
    tasks = await task_repo.list_by_team(team_id)
    if tenant.is_admin_or_owner:
        return [serialize_task(t) for t in tasks]
    # Team members see their own tasks plus unassigned team To-Dos — tasks are
    # created without an assignee and must be visible to the whole team.
    return [serialize_task(t) for t in tasks if t.assignee_id == tenant.user.id or t.assignee_id is None]


# ── Get / update / delete single task ────────────────────────────────────────

@router.get("/{task_id}", response_model=TaskDetailRead)
async def get_task(task_id: int, tenant: TenantContext = Depends(get_tenant_context)):
    task = await get_task_or_404(tenant, task_id)
    if tenant.is_admin_or_owner or task.assignee_id == tenant.user.id:
        return serialize_task(task)
    # A Project Manager may view any task in a project they're assigned to
    # manage, not only tasks personally assigned to them — otherwise they
    # couldn't see most of the work happening in a project they manage.
    if task.project_id is not None and tenant.has_project_manager_access:
        project_repo = ProjectRepository(tenant.db, tenant.organization_id)
        if await project_repo.is_member(task.project_id, tenant.user.id):
            return serialize_task(task)
    raise HTTPException(status_code=http_status.HTTP_403_FORBIDDEN, detail="You can only view your own tasks.")


@router.patch("/{task_id}/status", response_model=TaskDetailRead)
async def update_task_status(
    task_id: int,
    payload: TaskStatusUpdate,
    background_tasks: BackgroundTasks,
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db),
):
    repo = TaskRepository(db, tenant.organization_id)
    task = await get_task_or_404(tenant, task_id)

    if tenant.org_role == TEAM_MEMBER:
        if task.assignee_id != tenant.user.id:
            raise HTTPException(status_code=http_status.HTTP_403_FORBIDDEN, detail="You can only update status on tasks assigned to you.")

        if payload.status == "done":
            task.status = "pending_review"
            task.completed_by_id = tenant.user.id
            task.completed_at = datetime.now(timezone.utc)
            task.reviewed_by_id = None
            task.reviewed_at = None
            task.review_note = None

            team = task.team
            if team and team.team_manager_id:
                await create_notification(db=db, user_id=team.team_manager_id, task_id=task.id, title="Task pending review", message=f"{tenant.user.full_name} marked '{task.name}' as completed. Please review it.", type_="task_review")

            await db.commit()
            await db.refresh(task)

            user_repo = UserRepository(db)
            reviewer: User | None = None
            if team and team.team_manager_id:
                reviewer = await user_repo.get_by_id(team.team_manager_id)
            if reviewer is None:
                membership_result = await db.execute(select(TeamMembership).where(TeamMembership.user_id == tenant.user.id).limit(1))
                membership = membership_result.scalar_one_or_none()
                if membership:
                    member_team_result = await db.execute(select(Team).where(Team.id == membership.team_id))
                    member_team = member_team_result.scalar_one_or_none()
                    if member_team and member_team.team_manager_id:
                        reviewer = await user_repo.get_by_id(member_team.team_manager_id)
            if reviewer:
                background_tasks.add_task(bg_send_task_sent_for_review, task.id, tenant.user.id, reviewer.id)
            return serialize_task(task)

        if payload.status not in {"todo", "in_progress"}:
            raise HTTPException(status_code=http_status.HTTP_403_FORBIDDEN, detail="Members can only move tasks to Todo, In Progress, or submit Done for review.")

        updated = await repo.update(task, TaskUpdate(status=payload.status))
        return serialize_task(updated)

    if not tenant.is_admin_or_owner:
        raise HTTPException(status_code=http_status.HTTP_403_FORBIDDEN, detail="You are not allowed to update tasks.")

    if payload.status == "done" and task.status != "done":
        task.completed_by_id = task.assignee_id or tenant.user.id
        task.completed_at = datetime.now(timezone.utc)
        task.reviewed_by_id = tenant.user.id
        task.reviewed_at = datetime.now(timezone.utc)
        task.review_note = "Marked done directly."
    elif payload.status != "done" and task.status == "done":
        task.completed_by_id = None
        task.completed_at = None
        task.reviewed_by_id = None
        task.reviewed_at = None
        task.review_note = None

    updated = await repo.update(task, TaskUpdate(status=payload.status))
    return serialize_task(updated)


@router.post("/{task_id}/approve", response_model=TaskDetailRead)
async def approve_task(
    task_id: int,
    background_tasks: BackgroundTasks,
    tenant: TenantContext = Depends(require_org_manager),
    db: AsyncSession = Depends(get_db),
):
    task = await get_task_or_404(tenant, task_id)
    if task.status != "pending_review":
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail="Only pending review tasks can be approved.")

    completed_by_id = task.completed_by_id
    task.status = "done"
    task.reviewed_by_id = tenant.user.id
    task.reviewed_at = datetime.now(timezone.utc)
    task.review_note = "Approved"

    if completed_by_id:
        await create_notification(db=db, user_id=completed_by_id, task_id=task.id, title="Task approved", message=f"Your task '{task.name}' was approved.", type_="task_approved")

    await db.commit()
    await db.refresh(task)

    if completed_by_id:
        recipient = await UserRepository(db).get_by_id(completed_by_id)
        if recipient:
            background_tasks.add_task(bg_send_task_approved, task.id, recipient.id, tenant.user.id)
    return serialize_task(task)


@router.post("/{task_id}/assign-back", response_model=TaskDetailRead)
async def assign_task_back(
    task_id: int,
    payload: AssignBackRequest,
    background_tasks: BackgroundTasks,
    tenant: TenantContext = Depends(require_org_manager),
    db: AsyncSession = Depends(get_db),
):
    task = await get_task_or_404(tenant, task_id)
    if task.status != "pending_review":
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail="Only pending review tasks can be assigned back.")

    note = payload.note or "Assigned back for more work."
    task.status = "in_progress"
    task.reviewed_by_id = tenant.user.id
    task.reviewed_at = datetime.now(timezone.utc)
    task.review_note = note

    if task.assignee_id:
        await create_notification(db=db, user_id=task.assignee_id, task_id=task.id, title="Task assigned back", message=f"Your task '{task.name}' was assigned back. Reason: {note}", type_="task_assigned_back")

    await db.commit()
    await db.refresh(task)

    if task.assignee:
        background_tasks.add_task(bg_send_task_assigned_back, task.id, task.assignee.id, tenant.user.id, note)
    return serialize_task(task)


@router.patch("/{task_id}", response_model=TaskDetailRead)
async def update_task(
    task_id: int,
    payload: TaskUpdate,
    background_tasks: BackgroundTasks,
    tenant: TenantContext = Depends(require_org_manager),
    db: AsyncSession = Depends(get_db),
):
    repo = TaskRepository(db, tenant.organization_id)
    task = await get_task_or_404(tenant, task_id)

    if payload.team_id is not None:
        if await TeamRepository(db, tenant.organization_id).get_by_id(payload.team_id) is None:
            raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail="Selected team is invalid.")

    old_assignee_id = task.assignee_id
    old_due_date = task.due_date

    if payload.status == "done" and task.status != "done":
        task.completed_by_id = task.assignee_id or tenant.user.id
        task.completed_at = datetime.now(timezone.utc)
        task.reviewed_by_id = tenant.user.id
        task.reviewed_at = datetime.now(timezone.utc)
        task.review_note = "Marked done directly."
    elif payload.status is not None and payload.status != "done" and task.status == "done":
        task.completed_by_id = None
        task.completed_at = None
        task.reviewed_by_id = None
        task.reviewed_at = None
        task.review_note = None

    updated = await repo.update(task, payload)

    new_assignee_id = payload.assignee_id
    assignee_changed = new_assignee_id is not None and new_assignee_id != old_assignee_id

    if assignee_changed and new_assignee_id != tenant.user.id:
        await create_notification(db=db, user_id=new_assignee_id, task_id=updated.id, title="Task assigned to you", message=f"You have been assigned task: '{updated.name}'.", type_="task_assigned")
        await db.commit()
        new_assignee_user = await UserRepository(db).get_by_id(new_assignee_id)
        if new_assignee_user:
            background_tasks.add_task(bg_send_task_assigned, updated.id, new_assignee_user.id, tenant.user.id)

    if not assignee_changed and old_due_date != updated.due_date and updated.assignee:
        background_tasks.add_task(bg_send_due_date_updated, updated.id, updated.assignee.id, tenant.user.id, str(old_due_date) if old_due_date else None)

    return serialize_task(updated)


@router.delete("/{task_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_task(task_id: int, tenant: TenantContext = Depends(require_org_manager)):
    task = await get_task_or_404(tenant, task_id)
    await TaskRepository(tenant.db, tenant.organization_id).delete(task)
    return None
