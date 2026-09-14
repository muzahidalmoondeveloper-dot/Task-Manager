"""Scoreboard domain tools, manager/org tier (architecture item 3 —
"complete... Scoreboard capabilities required by the architecture", beyond
read_tools.py's existing get_my_scoreboard).

Scoreboard authorization follow-up (current product rule): Scoreboard is
an ADMIN-ONLY feature — access depends solely on the canonical Admin
capability, the same rule the HTTP scoreboard routes now enforce via
`require_org_admin` (app/api/routes/scoreboard.py,
app/api/routes/team_scoreboard.py, app/api/routes/organization_scoreboard.py).
Team Manager/Project Manager/Team Member/self access grants — this file's
previous ABAC replica of the OLD HTTP rule — no longer apply; PM/TM
capability alone (or combined) never grants Scoreboard access.

KNOWN LIMITATION: `ToolContext` (app.services.copilot.tools.registry) only
carries the base `org_role` string, not the granted-privilege `is_org_admin`
flag `TenantContext.is_admin_or_owner` also honors at the HTTP layer — that
flag isn't threaded through the chat/copilot tool-context construction
anywhere in this codebase today, and wiring it through is a larger,
unrelated change out of scope for this authorization + crash fix. A hybrid
user who holds Scoreboard access ONLY via the granted `is_org_admin` flag
(not the "owner"/"admin" role string) will therefore still be denied
Scoreboard access through chat specifically, even though the HTTP API
correctly allows them — flagged here as a follow-up, not silently
papered over."""

from app.repositories.team_repository import TeamRepository
from app.services.copilot.tools.read_schemas import GetOrgScoreboardInput, GetTeamScoreboardInput
from app.services.copilot.tools.registry import ToolContext, ToolResult, ToolSpec, register_tool

_ADMIN_ROLES = {"owner", "admin"}


async def _get_team_scoreboard_handler(ctx: ToolContext, params: GetTeamScoreboardInput) -> ToolResult:
    from app.services.scoreboard_service import build_team_scoreboard

    if ctx.org_role not in _ADMIN_ROLES:
        return ToolResult(False, "You don't have permission to view that team's scoreboard.")

    team_repo = TeamRepository(ctx.db, ctx.org_id)
    team = await team_repo.get_by_id(params.team_id)
    if team is None:
        return ToolResult(False, f"I couldn't find team #{params.team_id}.")

    data = await build_team_scoreboard(ctx.db, ctx.org_id, team, params.period)
    return ToolResult(True, "Team scoreboard retrieved.", data={"scoreboard": data, "team_name": team.name})


async def _get_org_scoreboard_handler(ctx: ToolContext, params: GetOrgScoreboardInput) -> ToolResult:
    from app.services.scoreboard_service import build_organization_scoreboard

    if ctx.org_role not in _ADMIN_ROLES:
        return ToolResult(False, "You don't have permission to view the company scoreboard.")

    team_repo = TeamRepository(ctx.db, ctx.org_id)
    teams = await team_repo.list_all()

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
