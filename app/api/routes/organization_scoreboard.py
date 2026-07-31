from datetime import date

from fastapi import APIRouter, Depends, Query
from fastapi import status as http_status

from app.core.auth_errors import AppException, ErrorDef
from app.core.tenant import TenantContext, get_tenant_context
from app.repositories.team_repository import TeamRepository
from app.schemas.scoreboard import (
    ManagerRankingResponse,
    ManagerRankingRow,
    OrgScoreboardEmployeeRow,
    OrgScoreboardResponse,
    TeamRankingResponse,
    TeamRankingRow,
)
from app.services import scoreboard_service as scoring

router = APIRouter(prefix="/scoreboard", tags=["Scoreboard"])

_FORBIDDEN = ErrorDef(code="ORG_SCOREBOARD_FORBIDDEN", status=http_status.HTTP_403_FORBIDDEN, message="You do not have permission to view the company scoreboard.")
_INVALID_PERIOD = ErrorDef(code="SCOREBOARD_INVALID_PERIOD", status=http_status.HTTP_400_BAD_REQUEST, message="Invalid period or date range.")


async def _visible_teams(tenant: TenantContext):
    """Owner/Admin see every team in the org; Team Managers see only the
    team(s) they manage. Everyone else is forbidden from all three rankings —
    there is no per-employee self-view here; that's `GET /users/{id}/scoreboard`."""
    if not tenant.is_manager_or_above:
        raise AppException(_FORBIDDEN)

    team_repo = TeamRepository(tenant.db, tenant.organization_id)
    if tenant.is_admin_or_owner:
        return await team_repo.list_all()
    return await team_repo.list_for_manager(tenant.user.id)


@router.get("/employees", response_model=OrgScoreboardResponse)
async def get_organization_scoreboard(
    period: str = Query("this_month"),
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    manager_id: int | None = Query(None),
    team_id: int | None = Query(None),
    employee_id: int | None = Query(None),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Individual employee leaderboard."""
    teams = await _visible_teams(tenant)

    try:
        result_period, period_start, period_end, rows = await scoring.build_organization_scoreboard(
            tenant.db, tenant.organization_id, teams, period,
            manager_id=manager_id, team_id=team_id, employee_id=employee_id,
            start_date=start_date, end_date=end_date,
        )
    except ValueError:
        raise AppException(_INVALID_PERIOD)

    employees = [
        OrgScoreboardEmployeeRow(
            rank=row.rank,
            user_id=row.user_id,
            full_name=row.full_name,
            role=row.role,
            manager_id=row.manager_id,
            manager_name=row.manager_name,
            team_id=row.team_id,
            team_name=row.team_name,
            has_data=row.result.has_data,
            rounded_score=row.result.rounded_score if row.result.has_data else None,
            performance_level=row.result.performance_level if row.result.has_data else None,
            total_completed=row.result.total_completed,
            on_time_rate=row.result.on_time_rate,
            overdue=row.result.overdue,
        )
        for row in rows
    ]

    return OrgScoreboardResponse(
        period=result_period, period_start=period_start, period_end=period_end,
        employees=employees,
    )


@router.get("/teams", response_model=TeamRankingResponse)
async def get_team_rankings(
    period: str = Query("this_month"),
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Team leaderboard — every visible team ranked by its own team-wide score."""
    teams = await _visible_teams(tenant)

    try:
        result_period, period_start, period_end, rows = await scoring.build_team_rankings(
            tenant.db, tenant.organization_id, teams, period,
            start_date=start_date, end_date=end_date,
        )
    except ValueError:
        raise AppException(_INVALID_PERIOD)

    team_rows = [
        TeamRankingRow(
            rank=row.rank,
            team_id=row.team_id,
            team_name=row.team_name,
            manager_id=row.manager_id,
            manager_name=row.manager_name,
            member_count=row.member_count,
            has_data=row.result.has_data,
            rounded_score=row.result.rounded_score if row.result.has_data else None,
            performance_level=row.result.performance_level if row.result.has_data else None,
            total_completed=row.result.total_completed,
            on_time_rate=row.result.on_time_rate,
            overdue=row.result.overdue,
        )
        for row in rows
    ]

    return TeamRankingResponse(
        period=result_period, period_start=period_start, period_end=period_end,
        teams=team_rows,
    )


@router.get("/managers", response_model=ManagerRankingResponse)
async def get_manager_rankings(
    period: str = Query("this_month"),
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Manager leaderboard — every visible manager ranked by the combined
    score across every team and/or project they manage. The full manager
    roster (including anyone not currently assigned a team/project) and
    Project Manager assignments are both org-wide, so they're only included
    for Owner/Admin (whose team visibility is already org-wide) — a Team
    Manager's own restricted view stays scoped to their own team(s), same as
    before."""
    teams = await _visible_teams(tenant)
    project_manager_assignments = []
    all_manager_users = []
    if tenant.is_admin_or_owner:
        project_manager_assignments = await scoring.fetch_project_manager_assignments(tenant.db, tenant.organization_id)
        all_manager_users = await scoring.fetch_all_manager_capable_users(tenant.db, tenant.organization_id)

    try:
        result_period, period_start, period_end, rows = await scoring.build_manager_rankings(
            tenant.db, tenant.organization_id, teams, period,
            start_date=start_date, end_date=end_date,
            project_manager_assignments=project_manager_assignments,
            all_manager_users=all_manager_users,
        )
    except ValueError:
        raise AppException(_INVALID_PERIOD)

    manager_rows = [
        ManagerRankingRow(
            rank=row.rank,
            manager_id=row.manager_id,
            manager_name=row.manager_name,
            team_count=row.team_count,
            project_count=row.project_count,
            employee_count=row.employee_count,
            has_data=row.result.has_data,
            rounded_score=row.result.rounded_score if row.result.has_data else None,
            performance_level=row.result.performance_level if row.result.has_data else None,
            total_completed=row.result.total_completed,
            on_time_rate=row.result.on_time_rate,
            overdue=row.result.overdue,
        )
        for row in rows
    ]

    return ManagerRankingResponse(
        period=result_period, period_start=period_start, period_end=period_end,
        managers=manager_rows,
    )
