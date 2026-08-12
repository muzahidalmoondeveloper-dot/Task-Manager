"""Project/Team creation + lifecycle tools (architecture domain buildout —
strict acceptance audit finding: Projects/Teams were entirely read-only, no
write capability existed at all in chat; architecture item 2 — "complete
Projects/Teams/Meetings chatbot lifecycle capabilities"). Create and
single-field update/status are AUTO-tier (R2) — see schemas.py's
UpdateProjectInput/UpdateTeamInput docstring for exactly what's in and out
of scope, and why team-manager reassignment is a separate, CONFIRM-tier
tool (reassign_team_manager, in org_structure_confirm_tools.py) rather than
folded into this same AUTO tier.

RBAC matches the existing HTTP API's rule exactly (app.core.tenant.
require_org_manager: "Owner, Admin, or Team Manager — teams/projects/tasks
mutations") — not a separately invented role set.

Also replicates the HTTP route's billing/plan-limit gate exactly
(check_active_billing + max_projects/max_teams, INCLUDING purchased
extra_teams/extra_users add-ons — same authoritative rule as
app.core.tenant.TenantContext.plan_limits, not a stricter approximation of
it; see _check_plan_capacity)."""

from sqlalchemy import func, select

from app.core.org_roles import ADMIN, OWNER, TEAM_MANAGER
from app.core.plan_limits import get_plan_limits
from app.models.organization import Organization
from app.models.project import Project
from app.models.team import Team
from app.schemas.chat import ChatAction
from app.services.copilot.abac import check_project_scope
from app.services.copilot.tools.registry import ToolContext, ToolResult, ToolSpec, register_tool
from app.services.copilot.tools.schemas import (
    CreateProjectInput,
    CreateTeamInput,
    ReassignTeamManagerInput,
    UpdateProjectInput,
    UpdateTeamInput,
)

_ORG_STRUCTURE_WRITE_ROLES = frozenset({OWNER, ADMIN, TEAM_MANAGER})

_SUBSCRIPTION_BLOCKED_STATUSES = {"past_due", "incomplete_expired", "cancelled"}


async def _check_plan_capacity(ctx: ToolContext, *, count_field: str, model) -> str | None:
    """Returns a refusal message if creating one more `model` row would
    exceed the org's plan limit, or if the org's subscription is past due —
    or None if creation may proceed. Mirrors
    app.core.tenant.TenantContext.plan_limits exactly, including purchased
    extra_teams/extra_users add-on capacity — the same authoritative
    calculation the HTTP API uses, not an independently-approximated one
    (item 5 fix — "make plan-limit checks use the same authoritative
    subscription/add-on capacity rules as the normal HTTP application
    paths")."""
    org = (await ctx.db.execute(select(Organization).where(Organization.id == ctx.org_id))).scalar_one_or_none()
    if org is None:
        return "Something is misconfigured — organization not found."

    from app.models.organization import Subscription
    sub = (await ctx.db.execute(select(Subscription).where(Subscription.organization_id == ctx.org_id))).scalar_one_or_none()
    if sub is not None and sub.status in _SUBSCRIPTION_BLOCKED_STATUSES:
        return "Your organization's subscription is past due, so new teams/projects can't be created right now. Please contact your billing admin."

    extra_teams = sub.extra_teams if sub else 0
    extra_users = sub.extra_users if sub else 0
    limits = get_plan_limits(org.plan, extra_teams, extra_users)
    limit = getattr(limits, count_field)
    if limit == -1:
        return None
    current_count = (await ctx.db.execute(select(func.count()).select_from(model).where(model.organization_id == ctx.org_id))).scalar_one()
    if current_count >= limit:
        return f"Your plan allows up to {limit} {model.__tablename__} — please upgrade your plan to create more."
    return None


async def _create_project_handler(ctx: ToolContext, params: CreateProjectInput) -> ToolResult:
    capacity_refusal = await _check_plan_capacity(ctx, count_field="max_projects", model=Project)
    if capacity_refusal is not None:
        return ToolResult(False, capacity_refusal)

    project = Project(
        name=params.name.strip(), description=params.description,
        created_by_id=ctx.user_id, organization_id=ctx.org_id,
    )
    ctx.db.add(project)
    await ctx.db.flush()

    verify = (await ctx.db.execute(select(Project).where(Project.id == project.id))).scalar_one_or_none()
    if verify is None or verify.name != params.name.strip():
        return ToolResult(False, f'Something went wrong creating project "{params.name}" — please check the Projects page.')

    return ToolResult(
        True, f'Created project "{verify.name}".',
        actions=[ChatAction(type="project_created", label=f'Project created: "{verify.name}"', payload={"project_id": verify.id})],
        data={"project_id": verify.id},
    )


async def _create_team_handler(ctx: ToolContext, params: CreateTeamInput) -> ToolResult:
    capacity_refusal = await _check_plan_capacity(ctx, count_field="max_teams", model=Team)
    if capacity_refusal is not None:
        return ToolResult(False, capacity_refusal)

    team = Team(
        name=params.name.strip(), description=params.description, team_manager_id=params.team_manager_id,
        created_by_id=ctx.user_id, organization_id=ctx.org_id,
    )
    ctx.db.add(team)
    await ctx.db.flush()

    verify = (await ctx.db.execute(select(Team).where(Team.id == team.id))).scalar_one_or_none()
    if verify is None or verify.name != params.name.strip():
        return ToolResult(False, f'Something went wrong creating team "{params.name}" — please check the Teams page.')

    return ToolResult(
        True, f'Created team "{verify.name}".',
        actions=[ChatAction(type="team_created", label=f'Team created: "{verify.name}"', payload={"team_id": verify.id})],
        data={"team_id": verify.id},
    )


async def _update_project_handler(ctx: ToolContext, params: UpdateProjectInput) -> ToolResult:
    project = (await ctx.db.execute(
        select(Project).where(Project.id == params.project_id, Project.organization_id == ctx.org_id)
    )).scalar_one_or_none()
    if project is None:
        return ToolResult(False, f"I couldn't find project #{params.project_id}.")

    scope_refusal = await check_project_scope(
        ctx.db, org_id=ctx.org_id, org_role=ctx.org_role, user_id=ctx.user_id, project_id=project.id,
    )
    if scope_refusal is not None:
        return ToolResult(False, scope_refusal)

    if params.name is None and params.description is None and params.status is None:
        return ToolResult(False, "I understood you want to update the project but couldn't determine what to change.")

    if params.name is not None:
        project.name = params.name.strip()
    if params.description is not None:
        project.description = params.description
    if params.status is not None:
        project.status = params.status
    await ctx.db.flush()

    await ctx.db.refresh(project)
    if params.name is not None and project.name != params.name.strip():
        return ToolResult(False, "Verification failed — the new name did not persist as expected.")
    if params.status is not None and project.status != params.status:
        return ToolResult(False, "Verification failed — the status did not persist as expected.")

    changes = []
    if params.name is not None:
        changes.append(f"name={project.name}")
    if params.status is not None:
        changes.append(f"status={project.status}")
    if params.description is not None:
        changes.append("description updated")
    return ToolResult(
        True, f'Updated project "{project.name}": {", ".join(changes)}.',
        actions=[ChatAction(type="project_updated", label=f'Project updated: "{project.name}"', payload={"project_id": project.id})],
        data={"project_id": project.id},
    )


async def _update_team_handler(ctx: ToolContext, params: UpdateTeamInput) -> ToolResult:
    team = (await ctx.db.execute(
        select(Team).where(Team.id == params.team_id, Team.organization_id == ctx.org_id)
    )).scalar_one_or_none()
    if team is None:
        return ToolResult(False, f"I couldn't find team #{params.team_id}.")

    if params.name is None and params.description is None:
        return ToolResult(False, "I understood you want to update the team but couldn't determine what to change.")

    if params.name is not None:
        team.name = params.name.strip()
    if params.description is not None:
        team.description = params.description
    await ctx.db.flush()

    await ctx.db.refresh(team)
    if params.name is not None and team.name != params.name.strip():
        return ToolResult(False, "Verification failed — the new name did not persist as expected.")

    changes = []
    if params.name is not None:
        changes.append(f"name={team.name}")
    if params.description is not None:
        changes.append("description updated")
    return ToolResult(
        True, f'Updated team "{team.name}": {", ".join(changes)}.',
        actions=[ChatAction(type="team_updated", label=f'Team updated: "{team.name}"', payload={"team_id": team.id})],
        data={"team_id": team.id},
    )


register_tool(ToolSpec(
    name="create_project",
    description="Create a new project.",
    input_schema=CreateProjectInput,
    handler=_create_project_handler,
    allowed_roles=_ORG_STRUCTURE_WRITE_ROLES,
))

register_tool(ToolSpec(
    name="create_team",
    description="Create a new team with a designated team manager.",
    input_schema=CreateTeamInput,
    handler=_create_team_handler,
    allowed_roles=_ORG_STRUCTURE_WRITE_ROLES,
))

register_tool(ToolSpec(
    name="update_project",
    description="Update a project's name, description, or status (active/paused/completed/cancelled). 'Archive' a project means setting its status to cancelled.",
    input_schema=UpdateProjectInput,
    handler=_update_project_handler,
    allowed_roles=_ORG_STRUCTURE_WRITE_ROLES,
))

register_tool(ToolSpec(
    name="update_team",
    description="Update a team's name or description.",
    input_schema=UpdateTeamInput,
    handler=_update_team_handler,
    allowed_roles=_ORG_STRUCTURE_WRITE_ROLES,
))

# CONFIRM-tier (R3) — no handler here; chat_service.py builds a change set
# via change_sets.build_change_set_for_entities() and app.services.copilot.
# transaction.execute_confirmed_change_set() applies it (see
# transaction._apply_reassign_team_manager). Registered here anyway so
# check_write_authorized()'s coarse RBAC pre-filter (allowed_roles) applies
# identically whether the tool is dispatched via run_tool() or the
# CONFIRM-tier change-set path — the same reasoning documented on
# ToolSpec.allowed_roles itself.
register_tool(ToolSpec(
    name="reassign_team_manager",
    description="Change which user manages a team.",
    input_schema=ReassignTeamManagerInput,
    handler=None,
    allowed_roles=_ORG_STRUCTURE_WRITE_ROLES,
))
