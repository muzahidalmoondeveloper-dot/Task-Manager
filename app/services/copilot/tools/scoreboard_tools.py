"""Scoreboard domain tools, manager/org tier (architecture item 3 —
"complete... Scoreboard capabilities required by the architecture", beyond
read_tools.py's existing get_my_scoreboard which is deliberately "my own
score only" — see that tool's own docstring for why viewing a COLLEAGUE's
score is a distinct, ABAC-gated capability that needed its own tool rather
than an optional employee_id param bolted onto get_my_scoreboard).

Both tools below replicate the EXACT same ABAC rules the HTTP scoreboard
routes already enforce (app/api/routes/team_scoreboard.py's
_require_can_view_team_scoreboard, app/api/routes/organization_scoreboard.
py's _visible_teams) rather than a chat-specific approximation of them —
same reasoning as this session's plan-limit fix (item 5): the chat surface
must use the SAME authoritative rule the normal HTTP application path uses,
not a separately-maintained one that can drift out of sync."""

from app.core.org_roles import PROJECT_MANAGER, TEAM_MANAGER, TEAM_MEMBER
from app.repositories.project_repository import ProjectRepository
from app.repositories.task_repository import TaskRepository
from app.repositories.team_repository import TeamRepository
from app.services.copilot.tools.read_schemas import GetOrgScoreboardInput, GetTeamScoreboardInput
from app.services.copilot.tools.registry import ToolContext, ToolResult, ToolSpec, register_tool


async def _can_view_team_scoreboard(ctx: ToolContext, team) -> bool:
    if ctx.org_role in {"owner", "admin"}:
        return True
    if ctx.org_role == TEAM_MANAGER:
        return team.team_manager_id == ctx.user_id
    if ctx.org_role == TEAM_MEMBER:
        return any(m.user_id == ctx.user_id for m in team.memberships)
    if ctx.org_role == PROJECT_MANAGER:
        task_repo = TaskRepository(ctx.db, ctx.org_id)
        team_tasks = await task_repo.list_by_team(team.id)
        project_ids = {t.project_id for t in team_tasks if t.project_id is not None}
        project_repo = ProjectRepository(ctx.db, ctx.org_id)
        for project_id in project_ids:
            if await project_repo.is_member(project_id, ctx.user_id):
                return True
        return False
    return False


async def _get_team_scoreboard_handler(ctx: ToolContext, params: GetTeamScoreboardInput) -> ToolResult:
    from app.services.scoreboard_service import build_team_scoreboard

    team_repo = TeamRepository(ctx.db, ctx.org_id)
    team = await team_repo.get_by_id(params.team_id)
    if team is None:
        return ToolResult(False, f"I couldn't find team #{params.team_id}.")

    if not await _can_view_team_scoreboard(ctx, team):
        return ToolResult(False, "You don't have permission to view that team's scoreboard.")

    data = await build_team_scoreboard(ctx.db, ctx.org_id, team, params.period)
    return ToolResult(True, "Team scoreboard retrieved.", data={"scoreboard": data, "team_name": team.name})


async def _get_org_scoreboard_handler(ctx: ToolContext, params: GetOrgScoreboardInput) -> ToolResult:
    from app.services.scoreboard_service import build_organization_scoreboard

    # Mirrors organization_scoreboard.py's _visible_teams exactly: only
    # manager-and-above may see any org-wide ranking at all; owner/admin see
    # every team, team_manager sees only the team(s) they manage.
    if ctx.org_role not in {"owner", "admin", TEAM_MANAGER}:
        return ToolResult(False, "You don't have permission to view the company scoreboard.")

    team_repo = TeamRepository(ctx.db, ctx.org_id)
    if ctx.org_role in {"owner", "admin"}:
        teams = await team_repo.list_all()
    else:
        teams = await team_repo.list_for_manager(ctx.user_id)

    try:
        result_period, period_start, period_end, rows = await build_organization_scoreboard(
            ctx.db, ctx.org_id, teams, params.period, team_id=params.team_id,
        )
    except ValueError:
        return ToolResult(False, "That's not a valid period.")

    return ToolResult(True, "Organization scoreboard retrieved.", data={"rows": rows, "period": result_period})


register_tool(ToolSpec(
    name="get_team_scoreboard",
    description="Get a specific team's performance scoreboard (current score, trend, per-member ranking) for a period.",
    input_schema=GetTeamScoreboardInput,
    handler=_get_team_scoreboard_handler,
    kind="read",
    client_blocked=True,
))

register_tool(ToolSpec(
    name="get_org_scoreboard",
    description="Get the organization-wide employee leaderboard for a period (managers and above only).",
    input_schema=GetOrgScoreboardInput,
    handler=_get_org_scoreboard_handler,
    kind="read",
    client_blocked=True,
))
