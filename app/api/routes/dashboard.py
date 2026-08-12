from fastapi import APIRouter, Depends

from app.api.routes.tasks import serialize_task
from app.core.tenant import TenantContext, get_tenant_context
from app.repositories.project_repository import ProjectRepository
from app.repositories.task_repository import TaskRepository
from app.repositories.team_repository import TeamRepository
from app.repositories.user_repository import UserRepository
from app.schemas.dashboard import DashboardSummaryRead, OrgTotals
from app.schemas.project import ProjectRead
from app.schemas.team import TeamRead

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


@router.get("/summary", response_model=DashboardSummaryRead)
async def get_dashboard_summary(tenant: TenantContext = Depends(get_tenant_context)):
    """A single role-aware endpoint backing the Dashboard/Overview page —
    each role gets tasks (and, where relevant, teams/projects) scoped to
    what they actually manage or are assigned to, computed server-side
    instead of the frontend fetching everything and filtering in-browser."""
    task_repo = TaskRepository(tenant.db, tenant.organization_id)
    team_repo = TeamRepository(tenant.db, tenant.organization_id)
    project_repo = ProjectRepository(tenant.db, tenant.organization_id)

    if tenant.is_admin_or_owner:
        user_repo = UserRepository(tenant.db)
        tasks, users, teams, projects = (
            await task_repo.list_all(),
            await user_repo.list_by_org(tenant.organization_id),
            await team_repo.list_all(),
            await project_repo.list_all(),
        )
        return DashboardSummaryRead(
            role_view="admin",
            tasks=[serialize_task(t) for t in tasks],
            teams=[TeamRead.model_validate(t) for t in teams],
            projects=[ProjectRead.model_validate(p) for p in projects],
            org_totals=OrgTotals(users=len(users), teams=len(teams), projects=len(projects)),
        )

    # Additive flags — a user can be both team-manager-capable and
    # project-manager-capable at once, so their dashboard is the union of
    # both scopes rather than picking just one.
    is_team_manager_capable = tenant.is_manager_or_above
    is_pm_capable = tenant.has_project_manager_access

    if is_team_manager_capable or is_pm_capable:
        managed_teams = await team_repo.list_for_manager(tenant.user.id) if is_team_manager_capable else []
        # ProjectMembership (not the `has_project_manager_access` role/flag)
        # is what actually grants project access now (see
        # app.core.project_access) — a Team Manager assigned as the
        # ProjectMembership "manager" of a specific project (without ever
        # being granted the project_manager role/flag) must see that
        # project here too, so this is queried for anyone in this branch,
        # not gated behind is_pm_capable. list_for_user() is a no-op
        # (empty list) for anyone with no project memberships at all, so
        # this is never a broadening for someone who truly has none.
        managed_projects = await project_repo.list_for_user(tenant.user.id)

        task_by_id = {}
        for t in await task_repo.list_by_team_ids([team.id for team in managed_teams]):
            task_by_id[t.id] = t
        for t in await task_repo.list_by_project_ids([p.id for p in managed_projects]):
            task_by_id[t.id] = t

        return DashboardSummaryRead(
            role_view="manager",
            tasks=[serialize_task(t) for t in task_by_id.values()],
            teams=[TeamRead.model_validate(t) for t in managed_teams],
            projects=[ProjectRead.model_validate(p) for p in managed_projects],
        )

    tasks = await task_repo.list_for_assignee(tenant.user.id)
    return DashboardSummaryRead(role_view="team_member", tasks=[serialize_task(t) for t in tasks])
