from datetime import date

from fastapi import APIRouter, Depends, Query
from fastapi import status as http_status

from app.core.auth_errors import AppException, ErrorDef
from app.core.org_roles import CLIENT
from app.core.tenant import TenantContext, get_tenant_context
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.task_repository import TaskRepository
from app.repositories.team_repository import TeamRepository
from app.repositories.user_repository import UserRepository
from app.schemas.scoreboard import (
    ScoreboardEmployee,
    ScoreboardResponse,
    ScoreboardSummary,
    ScoreboardTaskItem,
    ScoreBreakdown,
    ScoreHistoryPoint,
)
from app.services import scoreboard_service as scoring

router = APIRouter(prefix="/users/{user_id}/scoreboard", tags=["Scoreboard"])

_USER_NOT_FOUND = ErrorDef(code="USER_NOT_FOUND", status=http_status.HTTP_404_NOT_FOUND, message="User not found.")
_NOT_APPLICABLE = ErrorDef(code="SCOREBOARD_NOT_APPLICABLE", status=http_status.HTTP_400_BAD_REQUEST, message="Clients do not have a scoreboard.")
_FORBIDDEN = ErrorDef(code="SCOREBOARD_FORBIDDEN", status=http_status.HTTP_403_FORBIDDEN, message="You do not have permission to view this employee's scoreboard.")
_INVALID_PERIOD = ErrorDef(code="SCOREBOARD_INVALID_PERIOD", status=http_status.HTTP_400_BAD_REQUEST, message="Invalid period or date range.")

_PERIOD_LABELS = {
    "this_week": "This Week",
    "this_month": "This Month",
    "this_quarter": "This Quarter",
    "this_year": "This Year",
    "custom": "Custom Range",
}


async def _require_can_view_scoreboard(tenant: TenantContext, target_user_id: int) -> None:
    # Scoreboards only exist for staff — clients (and anyone no longer an
    # active member of this org) never have one, regardless of who's asking.
    org_repo = OrganizationRepository(tenant.db)
    target_membership = await org_repo.get_membership(tenant.organization_id, target_user_id)
    if target_membership is None or not target_membership.is_active:
        raise AppException(_USER_NOT_FOUND)
    if target_membership.role == CLIENT:
        raise AppException(_NOT_APPLICABLE)

    if tenant.user.id == target_user_id:
        return
    if tenant.is_admin_or_owner:
        return

    # Flag-aware (role OR granted privilege flag), and additive — a hybrid
    # Project Manager who's ALSO been granted team-manager privileges gets
    # the union of both checks below, not just one. Previously branched on
    # `tenant.org_role == "team_manager"` literally, which a user whose
    # base role is "project_manager" (even with the is_team_manager flag
    # granted and made an actual team's manager) could never match — they
    # fell into the project_manager branch instead, which checks a
    # completely different, project-membership-based criterion and
    # incorrectly returned 403 for someone who legitimately manages a team
    # the target user is on.
    if tenant.is_manager_or_above:
        team_repo = TeamRepository(tenant.db, tenant.organization_id)
        teams = await team_repo.list_for_manager(tenant.user.id)
        for team in teams:
            if any(m.user_id == target_user_id for m in team.memberships):
                return

    if tenant.has_project_manager_access:
        task_repo = TaskRepository(tenant.db, tenant.organization_id)
        target_tasks = await task_repo.list_for_assignee(target_user_id)
        project_ids = {t.project_id for t in target_tasks if t.project_id is not None}
        project_repo = ProjectRepository(tenant.db, tenant.organization_id)
        for project_id in project_ids:
            if await project_repo.is_member(project_id, tenant.user.id):
                return

    raise AppException(_FORBIDDEN)


async def _resolve_employee(tenant: TenantContext, user_id: int) -> ScoreboardEmployee:
    user_repo = UserRepository(tenant.db)
    employee = await user_repo.get_by_id(user_id)
    if employee is None:
        raise AppException(_USER_NOT_FOUND)

    team_repo = TeamRepository(tenant.db, tenant.organization_id)
    all_teams = await team_repo.list_all()
    teams = [
        t.name for t in all_teams
        if t.team_manager_id == user_id or any(m.user_id == user_id for m in t.memberships)
    ]

    return ScoreboardEmployee(
        id=employee.id, full_name=employee.full_name, email=employee.email,
        role=employee.role, teams=teams,
    )


@router.get("", response_model=ScoreboardResponse)
async def get_scoreboard(
    user_id: int,
    period: str = Query(default="this_month"),
    project_id: int | None = Query(default=None),
    team_id: int | None = Query(default=None),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    tenant: TenantContext = Depends(get_tenant_context),
):
    await _require_can_view_scoreboard(tenant, user_id)
    employee = await _resolve_employee(tenant, user_id)

    try:
        data = await scoring.build_employee_scoreboard(
            tenant.db, tenant.organization_id, user_id, period, project_id, team_id, start_date, end_date,
        )
    except ValueError as exc:
        raise AppException(_INVALID_PERIOD, message=str(exc))

    current = data.current
    return ScoreboardResponse(
        employee=employee,
        period=data.period,
        period_start=data.period_start,
        period_end=data.period_end,
        summary=ScoreboardSummary(
            total_assigned=current.total_assigned,
            total_completed=current.total_completed,
            completed_before_due=current.completed_before_due,
            completed_on_due=current.completed_on_due,
            completed_after_due=current.completed_after_due,
            completed_no_due_date=current.completed_no_due_date,
            overdue=current.overdue,
            pending=current.pending,
            completion_rate=current.completion_rate,
            on_time_rate=current.on_time_rate,
        ),
        score=ScoreBreakdown(
            has_data=current.has_data,
            completion_score=current.completion_score,
            on_time_score=current.on_time_score,
            overdue_score=current.overdue_score,
            total_score=current.total_score,
            rounded_score=current.rounded_score,
            performance_level=current.performance_level,
            change_from_previous=data.change_from_previous,
        ),
        explanation=data.explanation,
        trend=[ScoreHistoryPoint(**point) for point in data.trend],
    )


@router.get("/tasks", response_model=list[ScoreboardTaskItem])
async def get_scoreboard_tasks(
    user_id: int,
    period: str = Query(default="this_month"),
    project_id: int | None = Query(default=None),
    team_id: int | None = Query(default=None),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    tenant: TenantContext = Depends(get_tenant_context),
):
    await _require_can_view_scoreboard(tenant, user_id)

    try:
        period_start, period_end = scoring.resolve_period(period, start_date, end_date)
    except ValueError as exc:
        raise AppException(_INVALID_PERIOD, message=str(exc))

    items = await scoring.build_employee_task_items(
        tenant.db, tenant.organization_id, user_id, period_start, period_end, project_id, team_id,
    )
    return [ScoreboardTaskItem(**item) for item in items]
