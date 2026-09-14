from datetime import date

from fastapi import APIRouter, Depends, Query
from fastapi import status as http_status

from app.core.auth_errors import AppException, ErrorDef
from app.core.org_roles import CLIENT
from app.core.tenant import TenantContext, require_org_admin
from app.models.organization import OrganizationMembership
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


# ── Scoreboard authorization follow-up ──────────────────────────────────────
# Product rule (final): Scoreboard is an ADMIN-ONLY feature. Access depends
# solely on the existing, canonical `is_admin_or_owner` capability (Owner,
# Admin, or anyone additionally granted `is_org_admin` — the exact same
# capability `require_org_admin` already enforces everywhere else in this
# app, e.g. org settings/billing/member management). A Team Manager or
# Project Manager capability — alone or combined — NEVER grants Scoreboard
# access on its own; a hybrid PM/TM user who ALSO holds admin capability is
# allowed purely because of that admin capability, not because of PM/TM.
# Every route below is gated by `Depends(require_org_admin)` — the same
# dependency used elsewhere, not a second, Scoreboard-specific permission
# system.
#
# Previously, `_require_can_view_scoreboard` mixed TWO different concerns
# into one function: (1) is the VIEWER allowed to see this scoreboard at
# all (self/Owner/Admin/managing-Team-Manager/project-linked-PM), and (2)
# what IS the target user's own OrganizationMembership. Its Team-Manager
# branch had a bare `return` (no value) on every SUCCESSFUL access path,
# so callers received `None` instead of the target's membership; the
# caller (`_resolve_employee`) then dereferenced `.role` on `None` and
# crashed with `AttributeError: 'NoneType' object has no attribute
# 'role'` — a 500. `GET /scoreboard/tasks` never crashed because it only
# used the return value for a truthiness/side-effect check and never
# touched `.role`, which is why one endpoint 500'd and the sibling
# endpoint for the exact same user didn't.
#
# `_require_can_view_scoreboard` (and the equivalent
# `_require_can_view_team_scoreboard` in team_scoreboard.py) still exist,
# UNCHANGED, because `app.api.routes.reports` reuses them, as-is, for a
# DIFFERENT feature's authorization (Employee/Team Performance PDF
# Reports) — this task is scoped to the Scoreboard feature itself, not a
# redesign of Reports' own viewing rules, so those helpers — and the
# PM/TM/self eligibility they still encode for Reports' purposes — are
# deliberately left exactly as they were. The Scoreboard routes below no
# longer call them at all.
async def _resolve_target_membership(tenant: TenantContext, target_user_id: int) -> OrganizationMembership:
    """Resolves and validates the TARGET user's OrganizationMembership for
    the ACTIVE organization — deliberately the only thing this function
    does. Viewer authorization is a completely separate concern, handled
    by this router's own `Depends(require_org_admin)`; a function must
    never sometimes return a membership and sometimes return None for a
    caller to dereference unchecked — every return here is a real,
    active, non-Client OrganizationMembership, or this raises instead.
    Cross-tenant isolation is inherent: `get_membership` is scoped to
    `tenant.organization_id`, so a user who belongs only to a different
    organization simply has no row here and resolves as not found, never
    as another organization's data leaking through."""
    org_repo = OrganizationRepository(tenant.db)
    target_membership = await org_repo.get_membership(tenant.organization_id, target_user_id)
    if target_membership is None or not target_membership.is_active:
        raise AppException(_USER_NOT_FOUND)
    if target_membership.role == CLIENT:
        raise AppException(_NOT_APPLICABLE)
    return target_membership


async def _require_can_view_scoreboard(tenant: TenantContext, target_user_id: int) -> OrganizationMembership:
    """UNCHANGED — reused only by app.api.routes.reports for its own,
    separate Employee/Team Performance report-viewing rule (self/Owner/
    Admin/managing-Team-Manager/project-linked-PM). The Scoreboard routes
    in THIS file no longer call this; see `_resolve_target_membership` +
    `require_org_admin` above for the Scoreboard feature's own,
    Admin-only rule. Do not repurpose this function for Scoreboard access
    — Reports' behavior must stay exactly as it is."""
    target_membership = await _resolve_target_membership(tenant, target_user_id)

    if tenant.user.id == target_user_id:
        return target_membership
    if tenant.is_admin_or_owner:
        return target_membership

    if tenant.is_manager_or_above:
        team_repo = TeamRepository(tenant.db, tenant.organization_id)
        teams = await team_repo.list_for_manager(tenant.user.id)
        for team in teams:
            if any(m.user_id == target_user_id for m in team.memberships):
                return target_membership

    if tenant.has_project_manager_access:
        task_repo = TaskRepository(tenant.db, tenant.organization_id)
        target_tasks = await task_repo.list_for_assignee(target_user_id)
        project_ids = {t.project_id for t in target_tasks if t.project_id is not None}
        project_repo = ProjectRepository(tenant.db, tenant.organization_id)
        for project_id in project_ids:
            if await project_repo.is_member(project_id, tenant.user.id):
                return target_membership

    raise AppException(_FORBIDDEN)


async def _resolve_employee(tenant: TenantContext, user_id: int, membership: OrganizationMembership) -> ScoreboardEmployee:
    """Role-consistency fix (organization-role bug): `role` here MUST come
    from the caller-supplied `OrganizationMembership` (already resolved
    and scoped to THIS organization + this user by
    `_resolve_target_membership`) — never from the legacy, non-org-specific
    `User.role` column, which can silently go stale relative to a user's
    actual membership role (e.g. a user promoted to Owner in this org
    while `User.role` still reads "team_member"). This is exactly the
    field the User Detail / Scorecard header renders, so using the wrong
    source here is what previously made it disagree with the Users list
    (which already read membership.role correctly)."""
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
        role=membership.role, teams=teams,
    )


@router.get("", response_model=ScoreboardResponse)
async def get_scoreboard(
    user_id: int,
    period: str = Query(default="this_month"),
    project_id: int | None = Query(default=None),
    team_id: int | None = Query(default=None),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    tenant: TenantContext = Depends(require_org_admin),
):
    membership = await _resolve_target_membership(tenant, user_id)
    employee = await _resolve_employee(tenant, user_id, membership)

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
    tenant: TenantContext = Depends(require_org_admin),
):
    # Scoreboard authorization follow-up: previously this only called
    # _require_can_view_scoreboard for its side effect and never touched
    # `.role`, which is exactly why this endpoint never 500'd while the
    # sibling `GET ""` endpoint above did for the same user. Still
    # validates the target the same way every other Scoreboard endpoint
    # now does (real/active/non-Client, within this organization) — an
    # Admin must not be able to pull another organization's or a Client's
    # task data through this endpoint even though it doesn't need the
    # membership object itself.
    await _resolve_target_membership(tenant, user_id)

    try:
        period_start, period_end = scoring.resolve_period(period, start_date, end_date)
    except ValueError as exc:
        raise AppException(_INVALID_PERIOD, message=str(exc))

    items = await scoring.build_employee_task_items(
        tenant.db, tenant.organization_id, user_id, period_start, period_end, project_id, team_id,
    )
    return [ScoreboardTaskItem(**item) for item in items]
