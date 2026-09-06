import logging
from datetime import date, datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi import status as http_status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.activity_actions import (
    ENTITY_TASK,
    TASK_CREATED,
    TASK_DELETED,
    TASK_TIMER_STARTED,
    TASK_TIMER_STOPPED,
    TASK_UPDATED,
)
from app.core.auth_errors import AppException, ErrorDef
from app.core.database import get_db
from app.core.org_roles import PROJECT_MANAGER, TEAM_MEMBER
from app.core.project_access import is_project_scoped, list_project_team_ids, require_project_access
from app.core.task_assignment import validate_task_assignee
from app.core.tenant import TenantContext, get_tenant_context, require_org_admin, require_org_manager
from app.models.notification import Notification
from app.models.task import Task
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.repositories.project_repository import ProjectRepository
from app.repositories.task_repository import TaskRepository
from app.repositories.task_time_entry_repository import (
    DuplicateActiveTimerError,
    TaskTimeEntryRepository,
)
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
from app.schemas.task_time_entry import (
    TaskTimeState,
    TaskTimeSummariesRequest,
    TaskTimeSummariesResponse,
    TaskTimeSummaryItem,
)
from app.schemas.team import TeamRead
from app.schemas.user import UserRead
from app.services import activity_service
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

# Assignee-Only Timer Control follow-up.
_TASK_TIMER_ASSIGNEE_ONLY = ErrorDef(
    code="TASK_TIMER_ASSIGNEE_ONLY",
    status=http_status.HTTP_403_FORBIDDEN,
    message="Only the assigned user can start this task timer.",
)
_TASK_TIMER_ACTIVE = ErrorDef(
    code="TASK_TIMER_ACTIVE",
    status=http_status.HTTP_409_CONFLICT,
    message="Stop the active task timer before changing the assignee.",
)


def serialize_task(task: Task) -> TaskDetailRead:
    return TaskDetailRead(
        id=task.id,
        name=task.name,
        description=task.description,
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


async def can_access_task(tenant: TenantContext, task: Task) -> bool:
    """The single definitive "can this user see/work with this specific
    task" answer — used by GET /tasks/{id}, GET /tasks/{id}/time, and
    (as a base) time-tracking's start/stop routes, so read access never
    drifts into three inconsistent rules:
      - Admin/Owner: any task.
      - the task's assignee: their own task.
      - Project Manager access + membership on the task's project: any
        task under that project (mirrors "PM must see the work happening
        in a project they manage", not just tasks personally assigned).
      - Team Manager Task-scope follow-up: a plain Team Manager (role or
        granted `is_team_manager` flag) who actually MANAGES this task's
        team (`Team.team_manager_id == tenant.user.id` — not merely a
        TeamMembership row on it) may read any task filed under that
        team, exactly like a Project Manager reads any task under a
        project they're a member of. This does NOT extend to a plain
        team MEMBER who isn't the manager or assignee — that was never
        covered before and remains unchanged.
    Timer CONTROL (Start/Stop) is intentionally a separate, narrower
    question — see can_control_task_timer() below, which this function is
    never used for on its own."""
    if tenant.is_admin_or_owner or task.assignee_id == tenant.user.id:
        return True
    if task.project_id is not None and tenant.has_project_manager_access:
        project_repo = ProjectRepository(tenant.db, tenant.organization_id)
        if await project_repo.is_member(task.project_id, tenant.user.id):
            return True
    if task.team_id is not None and tenant.is_manager_or_above:
        team_repo = TeamRepository(tenant.db, tenant.organization_id)
        if await team_repo.is_manager(task.team_id, tenant.user.id):
            return True
    return False


def can_control_task_timer(task: Task, user_id: int) -> bool:
    """Assignee-Only Timer Control (follow-up to #7A): the ONLY question
    that grants permission to Start/Stop a Task's Working Timer — deliberately
    NOT `can_access_task()`, which answers a broader "can view/manage this
    task" question (Admin/Owner: any task; Project Manager: any task under
    a project they're assigned to). Timer control is strictly narrower:

        task.assignee_id is not None AND task.assignee_id == user_id

    No role overrides this. An Owner, Admin, Team Manager, or Project
    Manager who is not personally the assignee gets exactly the same
    `False` a random other Team Member would — the only way any of them
    gains timer control is by actually becoming the assignee (through the
    normal, separately-validated assignment flow), never through elevated
    privilege. An unassigned task (`assignee_id is None`) has no one who
    can start its timer at all."""
    return task.assignee_id is not None and task.assignee_id == user_id


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
    """"All Tasks" — Owner/Admin see every task in the org, unchanged.

    Team Manager Task-scope follow-up: `require_org_manager` above lets
    Owner, Admin, AND (base role or granted flag) Team Manager reach this
    endpoint — a plain Project Manager alone still cannot (matching their
    existing, unchanged scope: PM task listing is project-scoped via
    GET /tasks/project/{id}, never this org-wide endpoint). For anyone
    who is NOT Admin/Owner, this must never silently return every
    organization task — it's scoped to the team(s) they actually manage
    (`Team.team_manager_id`, never mere TeamMembership), unioned with any
    project(s) they're a genuine ProjectMembership member of if they ALSO
    hold Project Manager capability (the same "each capability
    contributes its own scope" combined-role model app.core.project_access
    already documents) — never expanded to the whole organization just
    because one of their roles happens to be Team Manager.
    """
    repo = TaskRepository(tenant.db, tenant.organization_id)

    scope_team_ids: set[int] | None = None
    scope_project_ids: set[int] | None = None
    if not tenant.is_admin_or_owner:
        team_repo = TeamRepository(tenant.db, tenant.organization_id)
        scope_team_ids = await team_repo.list_managed_team_ids(tenant.user.id)
        if tenant.has_project_manager_access:
            project_repo = ProjectRepository(tenant.db, tenant.organization_id)
            member_projects = await project_repo.list_for_user(tenant.user.id)
            scope_project_ids = {p.id for p in member_projects}

    tasks = await repo.list_all(
        status=status_filter, priority=priority_filter,
        project_id=project_id, team_id=team_id, assignee_id=assignee_id,
        due_date_from=due_date_from, due_date_to=due_date_to, overdue=overdue,
        scope_team_ids=scope_team_ids, scope_project_ids=scope_project_ids,
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

    # Task Assignee bug-fix follow-up: Client exclusion (Rule A),
    # organization-membership/active checks, and — when this task belongs
    # to a Team — the Team-membership boundary (Rule B/C), all enforced
    # here as the actual security boundary, after team_id itself has
    # already been confirmed to exist/be assignable above. Frontend
    # dropdown filtering is UX only; this is what a crafted request can't
    # bypass.
    await validate_task_assignee(
        db, organization_id=tenant.organization_id,
        assignee_id=payload.assignee_id, team_id=payload.team_id,
    )

    task = await task_repo.create(payload, created_by_id=tenant.user.id)

    await activity_service.record(
        db, organization_id=tenant.organization_id, actor=tenant.user,
        action=TASK_CREATED, entity_type=ENTITY_TASK, entity_id=task.id, entity_label=task.name,
    )

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
    if await can_access_task(tenant, task):
        return serialize_task(task)
    raise HTTPException(status_code=http_status.HTTP_403_FORBIDDEN, detail="You can only view your own tasks.")


async def _build_time_state(repo: TaskTimeEntryRepository, task: Task, tenant: TenantContext) -> TaskTimeState:
    """Fresh-computed on every call: DB-summed completed total for this
    task across ALL users, plus the live elapsed time of EVERY currently
    active session on this task across ALL users (#7A: one active timer
    per user, never one per task — more than one person can be timing the
    same task at once) — this is the task's true Working Time total, the
    same definition used everywhere else it's shown (see TaskTimeState's
    docstring). `is_active`/`active_started_at` are then picked back out
    for just the caller's own session, since that's what drives the
    Start/Stop button — never a cached/stale number, and never
    per-second persisted. `assignee_id` is carried straight through from
    `task` so the frontend can apply the assignee-only timer-control rule
    without a second request."""
    now = datetime.now(timezone.utc)
    total_completed = await repo.total_completed_seconds(task.id)
    active_entries = await repo.get_active_entries_for_task(task.id)

    active_elapsed_total = 0
    my_active = None
    for entry in active_entries:
        started_at = entry.started_at
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        active_elapsed_total += max(int((now - started_at).total_seconds()), 0)
        if entry.user_id == tenant.user.id:
            my_active = entry

    return TaskTimeState(
        task_id=task.id,
        tracked_time_seconds=total_completed + active_elapsed_total,
        is_active=my_active is not None,
        active_started_at=my_active.started_at if my_active is not None else None,
        active_timer_count=len(active_entries),
        assignee_id=task.assignee_id,
    )


@router.get("/{task_id}/time", response_model=TaskTimeState)
async def get_task_time_state(task_id: int, tenant: TenantContext = Depends(get_tenant_context)):
    """Read access stays exactly as broad as normal Task visibility
    (`can_access_task`) — Assignee-Only Timer Control (see
    `can_control_task_timer`) restricts who may START/STOP a timer, never
    who may see the Working Time total/Running state of a task they're
    already authorized to view (Phase 5 of that spec)."""
    task = await get_task_or_404(tenant, task_id)
    if not await can_access_task(tenant, task):
        raise HTTPException(status_code=http_status.HTTP_403_FORBIDDEN, detail="You can only view time tracked on your own tasks.")
    repo = TaskTimeEntryRepository(tenant.db, tenant.organization_id)
    return await _build_time_state(repo, task, tenant)


@router.post("/{task_id}/time/start", response_model=TaskTimeState, status_code=http_status.HTTP_201_CREATED)
async def start_task_timer(task_id: int, tenant: TenantContext = Depends(get_tenant_context)):
    """Starts a work session for the authenticated user on this task. The
    user identity is always `tenant.user.id` from the JWT — never accepted
    from the request body.

    Assignee-Only Timer Control: authorization here is deliberately
    `can_control_task_timer`, NOT the broader `can_access_task` — an
    Owner/Admin/Team Manager/Project Manager who can view or even edit
    this task is still rejected unless they are personally
    `task.assignee_id`. This is evaluated fresh against the current,
    just-loaded `task` row, so a stale frontend (e.g. showing a Start
    button for a task that was reassigned a moment ago) can never bypass
    this — the authoritative check always wins.

    A user may also have at most one active timer across the whole
    organization (existing #7A rule, unrelated to and unweakened by this
    check): starting a second one while another is running is rejected,
    not silently swapped."""
    task = await get_task_or_404(tenant, task_id)
    if not can_control_task_timer(task, tenant.user.id):
        raise AppException(_TASK_TIMER_ASSIGNEE_ONLY)

    repo = TaskTimeEntryRepository(tenant.db, tenant.organization_id)
    existing = await repo.get_active_for_user(tenant.user.id)
    if existing is not None:
        detail = (
            "You already have an active timer on this task."
            if existing.task_id == task_id
            else "You already have an active timer running on another task. Stop it before starting a new one."
        )
        raise AppException(ErrorDef(code="TIMER_ALREADY_ACTIVE", status=http_status.HTTP_409_CONFLICT, message=detail))

    try:
        await repo.start(task_id, tenant.user.id)
        await activity_service.record(
            tenant.db, organization_id=tenant.organization_id, actor=tenant.user,
            action=TASK_TIMER_STARTED, entity_type=ENTITY_TASK, entity_id=task_id, entity_label=task.name,
        )
    except DuplicateActiveTimerError:
        # The application-level check above found nothing, but a
        # concurrent request for this same user won the race at the
        # database's partial unique index — same outcome, reported the
        # same way, just caught one step later.
        raise AppException(ErrorDef(code="TIMER_ALREADY_ACTIVE", status=http_status.HTTP_409_CONFLICT, message="You already have an active timer running. Stop it before starting a new one."))

    return await _build_time_state(repo, task, tenant)


@router.post("/{task_id}/time/stop", response_model=TaskTimeState)
async def stop_task_timer(task_id: int, tenant: TenantContext = Depends(get_tenant_context)):
    """Stops the authenticated user's own active session on this task.
    There is deliberately no `entry_id` parameter — the target is always
    "my active session on this task", so User A can never stop User B's
    timer even by guessing/supplying an id.

    Assignee-Only Timer Control: the caller must satisfy BOTH conditions
    (Phase 4 of that spec) — (1) they are the task's CURRENT assignee
    (`can_control_task_timer`, re-checked fresh here, exactly like Start —
    a since-reassigned former assignee is rejected even if their own
    session is technically still active), AND (2) they own the active
    entry being stopped (`get_active_for_user_on_task`, pre-existing and
    unchanged). Neither check alone is sufficient: (1) without (2) would
    let a newly-reassigned assignee stop someone else's already-running
    entry, and (2) without (1) is the exact bug this follow-up closes."""
    task = await get_task_or_404(tenant, task_id)
    if not can_control_task_timer(task, tenant.user.id):
        raise AppException(_TASK_TIMER_ASSIGNEE_ONLY)

    repo = TaskTimeEntryRepository(tenant.db, tenant.organization_id)
    active = await repo.get_active_for_user_on_task(tenant.user.id, task_id)
    if active is None:
        raise AppException(ErrorDef(code="NO_ACTIVE_TIMER", status=http_status.HTTP_400_BAD_REQUEST, message="You don't have an active timer on this task."))

    stopped = await repo.stop(active)
    await activity_service.record(
        tenant.db, organization_id=tenant.organization_id, actor=tenant.user,
        action=TASK_TIMER_STOPPED, entity_type=ENTITY_TASK, entity_id=task_id, entity_label=task.name,
        metadata={"duration_seconds": stopped.duration_seconds},
    )
    return await _build_time_state(repo, task, tenant)


@router.post("/time-summaries", response_model=TaskTimeSummariesResponse)
async def get_task_time_summaries(
    payload: TaskTimeSummariesRequest,
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Bulk Working Time for however many tasks a list/table/card view has
    currently visible — ONE request, regardless of whether that's 10, 100,
    or 500 tasks (Task List Working Time follow-up). Never
    `GET /tasks/{id}/time` per row.

    Authorization mirrors `can_access_task` exactly (Admin/Owner: any task
    in the org; the task's own assignee; a Project Manager with
    ProjectMembership on the task's project; a Team Manager who actually
    manages the task's team — Team Manager Task-scope follow-up) but
    computed in bulk instead of once per task_id, so this stays a small,
    constant number of queries regardless of how many task_ids are
    requested:
      1. one query to fetch (id, assignee_id, project_id, team_id) for
         the requested ids, scoped to the caller's own organization —
         organization_id is never taken from the request.
      2. (only for a non-admin/owner caller with Project Manager access)
         one bulk membership query across every distinct candidate
         project_id, instead of one `is_member()` call per task.
      3. (only for a non-admin/owner caller — everyone who can reach this
         endpoint at all is at least Team-Manager-capable if they aren't
         Admin/Owner/pure Project Manager, same as `list_tasks`) one bulk
         "which of these team_ids do I manage" query, instead of one
         `is_manager()` call per task.
      4+5. the two-query bulk aggregation from
         TaskTimeEntryRepository.get_task_time_summaries.
      6. one single-row lookup for the caller's own org-wide active timer
         (Start/Stop-from-list follow-up) — always exactly one query,
         never one per task_id.

    A requested task_id that doesn't exist, belongs to another
    organization, or isn't one this caller is authorized to see is simply
    absent from `items` — never a 403/404 for the whole batch (matching
    how a partial-visibility batch request should behave), and never
    exposes *why* it's absent (existence vs. authorization are
    indistinguishable from the response, same as everywhere else task
    visibility is enforced in this app).
    """
    task_ids = list(dict.fromkeys(payload.task_ids))  # de-dupe, preserve order
    now = datetime.now(timezone.utc)
    if not task_ids:
        return TaskTimeSummariesResponse(calculated_at=now, items={})

    task_repo = TaskRepository(tenant.db, tenant.organization_id)
    candidates = await task_repo.list_scope_by_ids(task_ids)

    if tenant.is_admin_or_owner:
        allowed_ids = {task_id for task_id, _assignee_id, _project_id, _team_id in candidates}
    else:
        own_ids = {task_id for task_id, assignee_id, _project_id, _team_id in candidates if assignee_id == tenant.user.id}
        allowed_ids = set(own_ids)
        if tenant.has_project_manager_access:
            candidate_project_ids = {
                project_id for task_id, _assignee_id, project_id, _team_id in candidates
                if task_id not in own_ids and project_id is not None
            }
            if candidate_project_ids:
                project_repo = ProjectRepository(tenant.db, tenant.organization_id)
                member_project_ids = await project_repo.list_member_project_ids(list(candidate_project_ids), tenant.user.id)
                allowed_ids |= {
                    task_id for task_id, _assignee_id, project_id, _team_id in candidates
                    if project_id in member_project_ids
                }
        if tenant.is_manager_or_above:
            candidate_team_ids = {
                team_id for task_id, _assignee_id, _project_id, team_id in candidates
                if task_id not in allowed_ids and team_id is not None
            }
            if candidate_team_ids:
                team_repo = TeamRepository(tenant.db, tenant.organization_id)
                managed_team_ids = await team_repo.list_managed_team_ids(tenant.user.id)
                if managed_team_ids:
                    allowed_ids |= {
                        task_id for task_id, _assignee_id, _project_id, team_id in candidates
                        if team_id in managed_team_ids
                    }

    time_repo = TaskTimeEntryRepository(tenant.db, tenant.organization_id)
    summaries = await time_repo.get_task_time_summaries(list(allowed_ids), now)

    # One extra, constant-cost query (a single indexed row lookup on
    # user_id, backed by #7A's own partial unique index) — never a
    # per-task_id query — so the list can know up front whether the
    # CALLER (never a client-supplied user_id) is already timing
    # something anywhere in the org, and disable every other Start button
    # instead of letting the user discover a 409 only after clicking (see
    # Phase 3 of the Start/Stop-from-list follow-up).
    my_active_entry = await time_repo.get_active_for_user(tenant.user.id)
    my_active_task_id = my_active_entry.task_id if my_active_entry is not None else None

    items = {
        str(task_id): TaskTimeSummaryItem(
            working_time_seconds=summaries.get(task_id, (0, 0))[0],
            active_timer_count=summaries.get(task_id, (0, 0))[1],
            current_user_is_active=(task_id == my_active_task_id),
        )
        for task_id in allowed_ids
    }
    return TaskTimeSummariesResponse(
        calculated_at=now,
        items=items,
        current_user_has_active_timer=my_active_task_id is not None,
        # Only surfaced when it's actually one of the tasks in this
        # response — otherwise this list has nothing else to do with the
        # id (no name/row to attach it to), even though exposing it would
        # never be a privacy problem (it is always the caller's own task).
        current_user_active_task_id=my_active_task_id if my_active_task_id in allowed_ids else None,
    )


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

    # Task Assignee bug-fix follow-up: re-validate whenever the assignee
    # is being explicitly changed, OR whenever team_id is changing — the
    # latter matters even if assignee_id itself isn't in this PATCH body,
    # because moving a Task from Team A to Team B can leave its EXISTING
    # assignee (valid for Team A) no longer eligible for Team B. Per the
    # spec's "TEAM CHANGE + ASSIGNEE CHANGE": the resulting Task must never
    # be left in an invalid state, and assignee_id must never be silently
    # cleared — so this rejects the request instead, requiring the caller
    # to explicitly supply a valid assignee_id (or null) for the new team
    # in the same request.
    fields_set = payload.model_fields_set
    team_id_changing = "team_id" in fields_set and payload.team_id != task.team_id
    assignee_explicitly_set = "assignee_id" in fields_set
    if assignee_explicitly_set or team_id_changing:
        effective_team_id = payload.team_id if "team_id" in fields_set else task.team_id
        effective_assignee_id = payload.assignee_id if assignee_explicitly_set else task.assignee_id

        # Assignee-Only Timer Control follow-up (Phase 10-12): reassigning
        # or unassigning a Task while ANY of its TaskTimeEntry rows is
        # still active must be rejected outright if that change would
        # leave the active entry's owner no longer the Task's assignee —
        # otherwise the entry becomes an orphan nobody can Stop (the old
        # assignee no longer passes can_control_task_timer; the new
        # assignee doesn't own that entry). Never silently stop/delete/
        # transfer the entry — the caller must Stop the timer first, then
        # reassign, exactly like the spec's explicit "no silent fixups"
        # requirement for the Task Assignee rules this sits alongside.
        # Only checked when the assignee is actually about to change
        # (Phase 13: unrelated field updates — title/description/
        # priority/status/due date — must never be blocked by this).
        if effective_assignee_id != task.assignee_id:
            active_entries = await TaskTimeEntryRepository(db, tenant.organization_id).get_active_entries_for_task(task.id)
            if any(entry.user_id != effective_assignee_id for entry in active_entries):
                raise AppException(_TASK_TIMER_ACTIVE)

        await validate_task_assignee(
            db, organization_id=tenant.organization_id,
            assignee_id=effective_assignee_id, team_id=effective_team_id,
        )

    old_assignee_id = task.assignee_id
    old_due_date = task.due_date
    # Captured before repo.update() overwrites them — feeds the activity
    # log's "what actually changed" metadata below. Only small scalar
    # before/after pairs for a short allowlist of fields are recorded;
    # `description` intentionally only ever reports that it changed, never
    # its (potentially long) old/new text (Phase 9 of the spec).
    before = {"status": task.status, "priority": task.priority, "name": task.name, "assignee_id": task.assignee_id}

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

    # Flat before/after pairs only for the two headline fields the product
    # actually cares to show a transition for (matches the spec's own
    # `{"field": "status", "from": ..., "to": ...}` example shape);
    # everything else that changed is just named in `fields_changed` —
    # activity_service._sanitize_metadata() only persists flat scalar
    # values by design (never a nested object), so this shape is
    # deliberate, not a workaround.
    fields_changed: list[str] = []
    activity_metadata: dict = {}
    if updated.status != before["status"]:
        activity_metadata["status_from"], activity_metadata["status_to"] = before["status"], updated.status
        fields_changed.append("status")
    if updated.priority != before["priority"]:
        activity_metadata["priority_from"], activity_metadata["priority_to"] = before["priority"], updated.priority
        fields_changed.append("priority")
    if updated.assignee_id != before["assignee_id"]:
        activity_metadata["assignee_id_from"], activity_metadata["assignee_id_to"] = before["assignee_id"], updated.assignee_id
        fields_changed.append("assignee")
    if updated.name != before["name"]:
        fields_changed.append("name")
    if payload.description is not None:
        # Never the actual text — description is potentially long and is
        # the one field explicitly called out to summarize, not dump.
        fields_changed.append("description")
    if fields_changed:
        activity_metadata["fields_changed"] = fields_changed
    if activity_metadata:
        await activity_service.record(
            db, organization_id=tenant.organization_id, actor=tenant.user,
            action=TASK_UPDATED, entity_type=ENTITY_TASK, entity_id=updated.id, entity_label=updated.name,
            metadata=activity_metadata,
        )

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
    # Captured before the hard delete — Task is genuinely gone afterward
    # (no soft-delete), so this is the only chance to record a safe label
    # for the activity entry (entity_id/entity_type are plain columns, not
    # a destructive FK, so the log row itself survives regardless).
    task_id_value, task_name = task.id, task.name
    await TaskRepository(tenant.db, tenant.organization_id).delete(task)
    await activity_service.record(
        tenant.db, organization_id=tenant.organization_id, actor=tenant.user,
        action=TASK_DELETED, entity_type=ENTITY_TASK, entity_id=task_id_value, entity_label=task_name,
    )
    return None
