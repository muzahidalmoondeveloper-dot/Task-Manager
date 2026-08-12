"""Issue domain tools (architecture domain buildout — strict acceptance
audit finding: Issues were READ_ONLY, no write capability existed).
AUTO-tier (R2) — same risk tier as task create/update, no preview/confirm
needed for a single-record create or status change."""

from app.core.org_roles import ADMIN, OWNER, PROJECT_MANAGER, TEAM_MANAGER
from app.models.issue import Issue
from app.schemas.chat import ChatAction
from app.services.copilot.abac import check_project_scope
from app.services.copilot.tools.registry import ToolContext, ToolResult, ToolSpec, register_tool
from app.services.copilot.tools.schemas import CreateIssueInput, UpdateIssueStatusInput

_ISSUE_WRITE_ROLES = frozenset({OWNER, ADMIN, TEAM_MANAGER, PROJECT_MANAGER})


async def _create_issue_handler(ctx: ToolContext, params: CreateIssueInput) -> ToolResult:
    scope_refusal = await check_project_scope(
        ctx.db, org_id=ctx.org_id, org_role=ctx.org_role, user_id=ctx.user_id, project_id=params.project_id,
    )
    if scope_refusal is not None:
        return ToolResult(False, scope_refusal)

    issue = Issue(
        title=params.title.strip(),
        description=params.description,
        team_id=params.team_id,
        project_id=params.project_id,
        assignee_id=params.assignee_id,
        timeframe=params.timeframe,
        organization_id=ctx.org_id,
    )
    ctx.db.add(issue)
    await ctx.db.flush()

    # Post-execution verification (Section 30).
    from sqlalchemy import select
    verify = (await ctx.db.execute(select(Issue).where(Issue.id == issue.id))).scalar_one_or_none()
    if verify is None or verify.title != params.title.strip():
        return ToolResult(False, f'Something went wrong creating issue "{params.title}" — please check the Issues page.')

    return ToolResult(
        True, f'Created issue "{verify.title}".',
        actions=[ChatAction(type="issue_created", label=f'Issue created: "{verify.title}"', payload={"issue_id": verify.id})],
        data={"issue_id": verify.id},
    )


async def _update_issue_status_handler(ctx: ToolContext, params: UpdateIssueStatusInput) -> ToolResult:
    from sqlalchemy import select
    issue = (await ctx.db.execute(select(Issue).where(Issue.id == params.issue_id, Issue.organization_id == ctx.org_id))).scalar_one_or_none()
    if issue is None:
        return ToolResult(False, f"I couldn't find issue #{params.issue_id}.")

    scope_refusal = await check_project_scope(
        ctx.db, org_id=ctx.org_id, org_role=ctx.org_role, user_id=ctx.user_id, project_id=issue.project_id,
    )
    if scope_refusal is not None:
        return ToolResult(False, scope_refusal)

    if params.status not in ("open", "resolved"):
        return ToolResult(False, "Issue status must be 'open' or 'resolved'.")

    from datetime import datetime, timezone
    issue.status = params.status
    if params.resolution_plan is not None:
        issue.resolution_plan = params.resolution_plan
    issue.resolved_at = datetime.now(timezone.utc) if params.status == "resolved" else None
    await ctx.db.flush()

    await ctx.db.refresh(issue)
    if issue.status != params.status:
        return ToolResult(False, "Verification failed — issue status did not persist as expected.")

    return ToolResult(
        True, f'Updated issue "{issue.title}": status={params.status}.',
        actions=[ChatAction(type="issue_updated", label=f'Issue updated: "{issue.title}"', payload={"issue_id": issue.id})],
        data={"issue_id": issue.id},
    )


register_tool(ToolSpec(
    name="create_issue",
    description="Create a new issue for a team, optionally attached to a project. Use when the user explicitly asks to log/create/report an issue.",
    input_schema=CreateIssueInput,
    handler=_create_issue_handler,
    allowed_roles=_ISSUE_WRITE_ROLES,
))

register_tool(ToolSpec(
    name="update_issue_status",
    description="Change an issue's status (open/resolved), optionally recording a resolution plan.",
    input_schema=UpdateIssueStatusInput,
    handler=_update_issue_status_handler,
    allowed_roles=_ISSUE_WRITE_ROLES,
))
