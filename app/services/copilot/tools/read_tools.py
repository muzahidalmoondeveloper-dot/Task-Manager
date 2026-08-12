"""Read-only (search/list) domain tools for Rocks, Issues, KPIs, Meetings,
and Client Task Requests (architecture Section 25, read side).

These replace the previously-untyped, unaudited private query methods on
ChatService (`_query_rocks`/`_query_issues`/`_query_kpis`/`_query_meetings`/
`_query_client_requests`) with named, schema-typed, audited, centrally
policy-checked entry points — the exact same underlying SQL, moved here
verbatim rather than rewritten, since it was already correct (org-scoped,
already reviewed this session). ChatService's `_query_*` methods now
delegate to `run_tool()` (see chat_service.py) so every existing call site
and every existing sub-intent's response formatting is unchanged; only the
data-fetch step is now tool-mediated.

Every tool here is `kind="read"` — no risk gate, no change-set, no
optimistic-lock/verification step (nothing is mutated) — just:
    schema validation -> read authorization -> execute -> audit.
"""

from datetime import datetime, timezone

from sqlalchemy import select

from app.core.org_roles import ADMIN, CLIENT, OWNER, TEAM_MANAGER
from app.models.issue import Issue
from app.models.kpi import KPI
from app.models.meeting import Meeting
from app.models.rock import Rock
from app.models.task_request import TaskRequest
from app.repositories.project_repository import ProjectRepository
from app.repositories.team_repository import TeamRepository
from app.services.copilot.tools.read_schemas import (
    GetMyScoreboardInput,
    SearchClientRequestsInput,
    SearchIssuesInput,
    SearchKpisInput,
    SearchMeetingsInput,
    SearchProjectsInput,
    SearchRocksInput,
    SearchTeamsInput,
)
from app.services.copilot.tools.registry import ToolContext, ToolResult, ToolSpec, register_tool


async def _search_rocks_handler(ctx: ToolContext, params: SearchRocksInput) -> ToolResult:
    stmt = select(Rock).where(
        Rock.organization_id == ctx.org_id, Rock.is_archived.is_(False)
    ).order_by(Rock.created_at.desc())
    if params.team_id is not None:
        stmt = stmt.where(Rock.team_id == params.team_id)
    rows = list((await ctx.db.execute(stmt)).scalars().all())
    return ToolResult(True, f"{len(rows)} rock(s) found.", data={"items": rows})


async def _search_issues_handler(ctx: ToolContext, params: SearchIssuesInput) -> ToolResult:
    stmt = select(Issue).where(Issue.organization_id == ctx.org_id).order_by(Issue.created_at.desc())
    if params.open_only:
        stmt = stmt.where(Issue.status != "resolved")
    rows = list((await ctx.db.execute(stmt)).scalars().all())
    return ToolResult(True, f"{len(rows)} issue(s) found.", data={"items": rows})


async def _search_kpis_handler(ctx: ToolContext, params: SearchKpisInput) -> ToolResult:
    stmt = select(KPI).where(KPI.organization_id == ctx.org_id, KPI.is_snoozed.is_(False)).order_by(KPI.sort_order)
    if params.team_id is not None:
        stmt = stmt.where(KPI.team_id == params.team_id)
    rows = list((await ctx.db.execute(stmt)).scalars().all())
    return ToolResult(True, f"{len(rows)} KPI(s) found.", data={"items": rows})


async def _search_meetings_handler(ctx: ToolContext, params: SearchMeetingsInput) -> ToolResult:
    stmt = select(Meeting).where(Meeting.organization_id == ctx.org_id).order_by(Meeting.scheduled_at)
    if params.upcoming_only:
        stmt = stmt.where(Meeting.scheduled_at >= datetime.now(timezone.utc), Meeting.status == "scheduled")
    rows = list((await ctx.db.execute(stmt)).scalars().all())
    return ToolResult(True, f"{len(rows)} meeting(s) found.", data={"items": rows})


async def _search_client_requests_handler(ctx: ToolContext, params: SearchClientRequestsInput) -> ToolResult:
    # ABAC (master-prompt item 2): a client only ever sees their own
    # submitted requests, regardless of org — this scoping happens inside
    # the handler (needs the resolved caller identity), not the coarse
    # read-authorization gate, matching the existing rule this replaces.
    stmt = (
        select(TaskRequest)
        .where(TaskRequest.organization_id == ctx.org_id)
        .order_by(TaskRequest.created_at.desc())
    )
    if ctx.org_role == CLIENT:
        stmt = stmt.where(TaskRequest.submitted_by_id == ctx.user_id)
    elif ctx.org_role not in {OWNER, ADMIN, TEAM_MANAGER}:
        stmt = stmt.where(TaskRequest.submitted_by_id == ctx.user_id)
    rows = list((await ctx.db.execute(stmt)).scalars().all())
    return ToolResult(True, f"{len(rows)} request(s) found.", data={"items": rows})


async def _get_my_scoreboard_handler(ctx: ToolContext, params: GetMyScoreboardInput) -> ToolResult:
    # Domain buildout — Scoreboards (strict acceptance audit: 0% chat
    # coverage). Deliberately "my own score only" — scores are a computed,
    # read-only projection of task completion data (no write path exists or
    # should exist for them; see scoreboard_service.py, entirely derived),
    # and viewing a COLLEAGUE's score is a sensitive, managerial action this
    # pass does not extend chat into — that's a real ABAC dimension (who
    # can see whose score) this app's existing scoreboard API already
    # enforces at the HTTP layer that a chat tool would need to replicate
    # faithfully, not guess at.
    from app.services.scoreboard_service import build_employee_scoreboard

    data = await build_employee_scoreboard(ctx.db, ctx.org_id, ctx.user_id, params.period)
    return ToolResult(True, "Scoreboard retrieved.", data={"scoreboard": data})


async def _search_projects_handler(ctx: ToolContext, params: SearchProjectsInput) -> ToolResult:
    project_repo = ProjectRepository(ctx.db, ctx.org_id)
    rows = await project_repo.list_all()
    return ToolResult(True, f"{len(rows)} project(s) found.", data={"items": rows})


async def _search_teams_handler(ctx: ToolContext, params: SearchTeamsInput) -> ToolResult:
    team_repo = TeamRepository(ctx.db, ctx.org_id)
    rows = await team_repo.list_all()
    return ToolResult(True, f"{len(rows)} team(s) found.", data={"items": rows})


register_tool(ToolSpec(
    name="search_rocks",
    description="List Rocks (quarterly goals), optionally filtered to one team.",
    input_schema=SearchRocksInput,
    handler=_search_rocks_handler,
    kind="read",
    client_blocked=True,  # matches CLIENT_BLOCKED_SUB_INTENTS: rock_list, rock_by_team
))

register_tool(ToolSpec(
    name="search_issues",
    description="List Issues, optionally only the still-open ones.",
    input_schema=SearchIssuesInput,
    handler=_search_issues_handler,
    kind="read",
    client_blocked=True,  # matches CLIENT_BLOCKED_SUB_INTENTS: issue_list, issue_open
))

register_tool(ToolSpec(
    name="search_kpis",
    description="List KPIs (scoreboard metrics), optionally filtered to one team.",
    input_schema=SearchKpisInput,
    handler=_search_kpis_handler,
    kind="read",
    client_blocked=True,  # matches CLIENT_BLOCKED_SUB_INTENTS: kpi_list, kpi_progress
))

register_tool(ToolSpec(
    name="search_meetings",
    description="List meetings, optionally only upcoming ones.",
    input_schema=SearchMeetingsInput,
    handler=_search_meetings_handler,
    kind="read",
    client_blocked=True,  # matches CLIENT_BLOCKED_SUB_INTENTS: meeting_list, meeting_upcoming
))

register_tool(ToolSpec(
    name="search_client_requests",
    description="List client task requests. Clients see only their own; staff see the organization's.",
    input_schema=SearchClientRequestsInput,
    handler=_search_client_requests_handler,
    kind="read",
    client_blocked=False,  # client_request_list is intentionally NOT blocked for CLIENT
))

register_tool(ToolSpec(
    name="search_projects",
    description="List all projects in the organization (staff-only overview; use project_progress for a single named project instead).",
    input_schema=SearchProjectsInput,
    handler=_search_projects_handler,
    kind="read",
    client_blocked=True,  # matches CLIENT_BLOCKED_SUB_INTENTS: project_count, project_list, project_by_status
))

register_tool(ToolSpec(
    name="get_my_scoreboard",
    description="Get the requesting user's own performance scoreboard for a period (this_week/this_month/this_quarter/this_year).",
    input_schema=GetMyScoreboardInput,
    handler=_get_my_scoreboard_handler,
    kind="read",
    client_blocked=True,  # scoreboards are internal-staff performance data
))

register_tool(ToolSpec(
    name="search_teams",
    description="List all teams in the organization, or a named team's members/workload.",
    input_schema=SearchTeamsInput,
    handler=_search_teams_handler,
    kind="read",
    client_blocked=True,  # matches CLIENT_BLOCKED_SUB_INTENTS: team_count, team_list, team_members, team_workload
))
