"""Rock domain tools (architecture domain buildout — strict acceptance
audit finding: Rocks were READ_ONLY). AUTO-tier (R2)."""

from datetime import datetime, timezone

from sqlalchemy import select

from app.core.org_roles import ADMIN, OWNER, PROJECT_MANAGER, TEAM_MANAGER
from app.models.rock import Rock
from app.schemas.chat import ChatAction
from app.services.copilot.abac import check_project_scope
from app.services.copilot.tools.registry import ToolContext, ToolResult, ToolSpec, register_tool
from app.services.copilot.tools.schemas import CreateRockInput, UpdateRockStatusInput

_ROCK_WRITE_ROLES = frozenset({OWNER, ADMIN, TEAM_MANAGER, PROJECT_MANAGER})
_VALID_ROCK_STATUSES = {"backlog", "on_track", "at_risk", "off_track", "done"}


async def _create_rock_handler(ctx: ToolContext, params: CreateRockInput) -> ToolResult:
    scope_refusal = await check_project_scope(
        ctx.db, org_id=ctx.org_id, org_role=ctx.org_role, user_id=ctx.user_id, project_id=params.project_id,
    )
    if scope_refusal is not None:
        return ToolResult(False, scope_refusal)

    rock = Rock(
        title=params.title.strip(),
        description=params.description,
        team_id=params.team_id,
        project_id=params.project_id,
        owner_id=params.owner_id,
        due_date=params.due_date,
        organization_id=ctx.org_id,
    )
    ctx.db.add(rock)
    await ctx.db.flush()

    verify = (await ctx.db.execute(select(Rock).where(Rock.id == rock.id))).scalar_one_or_none()
    if verify is None or verify.title != params.title.strip():
        return ToolResult(False, f'Something went wrong creating rock "{params.title}" — please check the Rocks page.')

    return ToolResult(
        True, f'Created rock "{verify.title}".',
        actions=[ChatAction(type="rock_created", label=f'Rock created: "{verify.title}"', payload={"rock_id": verify.id})],
        data={"rock_id": verify.id},
    )


async def _update_rock_status_handler(ctx: ToolContext, params: UpdateRockStatusInput) -> ToolResult:
    rock = (await ctx.db.execute(select(Rock).where(Rock.id == params.rock_id, Rock.organization_id == ctx.org_id))).scalar_one_or_none()
    if rock is None:
        return ToolResult(False, f"I couldn't find rock #{params.rock_id}.")

    scope_refusal = await check_project_scope(
        ctx.db, org_id=ctx.org_id, org_role=ctx.org_role, user_id=ctx.user_id, project_id=rock.project_id,
    )
    if scope_refusal is not None:
        return ToolResult(False, scope_refusal)

    if params.status not in _VALID_ROCK_STATUSES:
        return ToolResult(False, f"Rock status must be one of: {', '.join(sorted(_VALID_ROCK_STATUSES))}.")

    rock.status = params.status
    await ctx.db.flush()

    await ctx.db.refresh(rock)
    if rock.status != params.status:
        return ToolResult(False, "Verification failed — rock status did not persist as expected.")

    return ToolResult(
        True, f'Updated rock "{rock.title}": status={params.status}.',
        actions=[ChatAction(type="rock_updated", label=f'Rock updated: "{rock.title}"', payload={"rock_id": rock.id})],
        data={"rock_id": rock.id},
    )


register_tool(ToolSpec(
    name="create_rock",
    description="Create a new quarterly Rock (goal) for a team, optionally attached to a project.",
    input_schema=CreateRockInput,
    handler=_create_rock_handler,
    allowed_roles=_ROCK_WRITE_ROLES,
))

register_tool(ToolSpec(
    name="update_rock_status",
    description="Change a Rock's status (backlog/on_track/at_risk/off_track/done).",
    input_schema=UpdateRockStatusInput,
    handler=_update_rock_status_handler,
    allowed_roles=_ROCK_WRITE_ROLES,
))
