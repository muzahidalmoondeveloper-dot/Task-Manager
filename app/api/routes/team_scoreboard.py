from datetime import date

from fastapi import APIRouter, Depends, Query
from fastapi import status as http_status

from app.core.auth_errors import AppException, ErrorDef
from app.core.tenant import TenantContext, get_tenant_context
from app.models.team import Team
from app.repositories.project_repository import ProjectRepository
from app.repositories.task_repository import TaskRepository
from app.repositories.team_repository import TeamRepository
from app.schemas.scoreboard import (
    ScoreboardSummary,
    ScoreboardTaskItem,
    ScoreBreakdown,
    ScoreHistoryPoint,
    TeamInfo,
    TeamScoreboardMemberRow,
    TeamScoreboardResponse,
)
from app.services import scoreboard_service as scoring
from app.services.scoreboard_service import ScoreboardResult

router = APIRouter(prefix="/teams/{team_id}/scoreboard", tags=["Team Scoreboard"])

_TEAM_NOT_FOUND = ErrorDef(code="TEAM_NOT_FOUND", status=http_status.HTTP_404_NOT_FOUND, message="Team not found.")
_FORBIDDEN = ErrorDef(code="TEAM_SCOREBOARD_FORBIDDEN", status=http_status.HTTP_403_FORBIDDEN, message="You do not have permission to view this team's scoreboard.")
_INVALID_PERIOD = ErrorDef(code="SCOREBOARD_INVALID_PERIOD", status=http_status.HTTP_400_BAD_REQUEST, message="Invalid period or date range.")


async def _get_team_or_404(tenant: TenantContext, team_id: int) -> Team:
    team_repo = TeamRepository(tenant.db, tenant.organization_id)
    team = await team_repo.get_by_id(team_id)
    if team is None:
        raise AppException(_TEAM_NOT_FOUND)
    return team


async def _require_can_view_team_scoreboard(tenant: TenantContext, team: Team) -> None:
    if tenant.is_admin_or_owner:
        return

    if tenant.org_role == "team_manager":
        if team.team_manager_id == tenant.user.id:
            return
        raise AppException(_FORBIDDEN)

    if tenant.org_role == "team_member":
        if any(m.user_id == tenant.user.id for m in team.memberships):
            return
        raise AppException(_FORBIDDEN)

    if tenant.org_role == "project_manager":
        task_repo = TaskRepository(tenant.db, tenant.organization_id)
        team_tasks = await task_repo.list_by_team(team.id)
        project_ids = {t.project_id for t in team_tasks if t.project_id is not None}
        project_repo = ProjectRepository(tenant.db, tenant.organization_id)
        for project_id in project_ids:
            if await project_repo.is_member(project_id, tenant.user.id):
                return
        raise AppException(_FORBIDDEN)

    raise AppException(_FORBIDDEN)


def _team_info(team: Team) -> TeamInfo:
    return TeamInfo(
        id=team.id,
        name=team.name,
        description=team.description,
        manager_name=team.team_manager.full_name if team.team_manager else None,
        member_count=len(team.memberships),
    )


def _to_summary(result: ScoreboardResult) -> ScoreboardSummary:
    return ScoreboardSummary(
        total_assigned=result.total_assigned,
        total_completed=result.total_completed,
        completed_before_due=result.completed_before_due,
        completed_on_due=result.completed_on_due,
        completed_after_due=result.completed_after_due,
        completed_no_due_date=result.completed_no_due_date,
        overdue=result.overdue,
        pending=result.pending,
        completion_rate=result.completion_rate,
        on_time_rate=result.on_time_rate,
    )


def _to_score(result: ScoreboardResult, change_from_previous: int | None) -> ScoreBreakdown:
    return ScoreBreakdown(
        has_data=result.has_data,
        completion_score=result.completion_score,
        on_time_score=result.on_time_score,
        overdue_score=result.overdue_score,
        total_score=result.total_score,
        rounded_score=result.rounded_score,
        performance_level=result.performance_level,
        change_from_previous=change_from_previous,
    )


@router.get("", response_model=TeamScoreboardResponse)
async def get_team_scoreboard(
    team_id: int,
    period: str = Query(default="this_month"),
    project_id: int | None = Query(default=None),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    tenant: TenantContext = Depends(get_tenant_context),
):
    team = await _get_team_or_404(tenant, team_id)
    await _require_can_view_team_scoreboard(tenant, team)

    try:
        data = await scoring.build_team_scoreboard(
            tenant.db, tenant.organization_id, team, period, project_id, start_date, end_date,
        )
    except ValueError as exc:
        raise AppException(_INVALID_PERIOD, message=str(exc))

    current = data.current
    member_rows = [
        TeamScoreboardMemberRow(
            rank=m.rank,
            user_id=m.user_id,
            full_name=m.full_name,
            role=m.role,
            has_data=m.result.has_data,
            rounded_score=m.result.rounded_score if m.result.has_data else None,
            performance_level=m.result.performance_level if m.result.has_data else None,
            total_assigned=m.result.total_assigned,
            total_completed=m.result.total_completed,
            overdue=m.result.overdue,
            completion_rate=m.result.completion_rate,
            on_time_rate=m.result.on_time_rate,
        )
        for m in data.members
    ]

    return TeamScoreboardResponse(
        team=_team_info(team),
        period=data.period,
        period_start=data.period_start,
        period_end=data.period_end,
        summary=_to_summary(current),
        score=_to_score(current, data.change_from_previous),
        members=member_rows,
        trend=[ScoreHistoryPoint(**point) for point in data.trend],
        previous_trend=[ScoreHistoryPoint(**point) for point in data.previous_trend],
        insights=data.insights,
    )


@router.get("/tasks", response_model=list[ScoreboardTaskItem])
async def get_team_scoreboard_tasks(
    team_id: int,
    period: str = Query(default="this_month"),
    project_id: int | None = Query(default=None),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    tenant: TenantContext = Depends(get_tenant_context),
):
    team = await _get_team_or_404(tenant, team_id)
    await _require_can_view_team_scoreboard(tenant, team)

    try:
        period_start, period_end = scoring.resolve_period(period, start_date, end_date)
    except ValueError as exc:
        raise AppException(_INVALID_PERIOD, message=str(exc))

    items = await scoring.build_team_task_items(
        tenant.db, tenant.organization_id, team_id, period_start, period_end, project_id,
    )
    return [ScoreboardTaskItem(**item) for item in items]
