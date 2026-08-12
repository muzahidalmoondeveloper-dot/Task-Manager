"""Meeting domain tools (architecture domain buildout — strict acceptance
audit finding: Meetings were READ_ONLY). AUTO-tier (R2), deliberately
narrow — see schemas.py's ScheduleMeetingInput/UpdateMeetingInput docstring
for exactly what's in and out of scope."""

from sqlalchemy import select

from app.core.org_roles import ADMIN, OWNER, PROJECT_MANAGER, TEAM_MANAGER
from app.models.meeting import Meeting
from app.schemas.chat import ChatAction
from app.services.copilot.abac import check_project_scope
from app.services.copilot.tools.registry import ToolContext, ToolResult, ToolSpec, register_tool
from app.services.copilot.tools.schemas import ScheduleMeetingInput, UpdateMeetingInput

_MEETING_WRITE_ROLES = frozenset({OWNER, ADMIN, TEAM_MANAGER, PROJECT_MANAGER})
# architecture item 2 — Meetings lifecycle: scheduled -> in_progress
# (started) -> completed (ended), or cancelled at any point before it ends.
_VALID_MEETING_STATUSES = {"scheduled", "in_progress", "completed", "cancelled"}


async def _schedule_meeting_handler(ctx: ToolContext, params: ScheduleMeetingInput) -> ToolResult:
    scope_refusal = await check_project_scope(
        ctx.db, org_id=ctx.org_id, org_role=ctx.org_role, user_id=ctx.user_id, project_id=params.project_id,
    )
    if scope_refusal is not None:
        return ToolResult(False, scope_refusal)

    meeting = Meeting(
        title=params.title.strip(),
        scheduled_at=params.scheduled_at,
        duration_minutes=params.duration_minutes,
        team_id=params.team_id,
        project_id=params.project_id,
        location=params.location,
        organizer_id=ctx.user_id,
        organization_id=ctx.org_id,
    )
    ctx.db.add(meeting)
    await ctx.db.flush()

    verify = (await ctx.db.execute(select(Meeting).where(Meeting.id == meeting.id))).scalar_one_or_none()
    if verify is None or verify.title != params.title.strip():
        return ToolResult(False, f'Something went wrong scheduling "{params.title}" — please check the Meetings page.')

    return ToolResult(
        True, f'Scheduled "{verify.title}" for {verify.scheduled_at.strftime("%Y-%m-%d %H:%M")}.',
        actions=[ChatAction(type="meeting_scheduled", label=f'Meeting scheduled: "{verify.title}"', payload={"meeting_id": verify.id})],
        data={"meeting_id": verify.id},
    )


async def _update_meeting_handler(ctx: ToolContext, params: UpdateMeetingInput) -> ToolResult:
    meeting = (await ctx.db.execute(select(Meeting).where(Meeting.id == params.meeting_id, Meeting.organization_id == ctx.org_id))).scalar_one_or_none()
    if meeting is None:
        return ToolResult(False, f"I couldn't find meeting #{params.meeting_id}.")

    scope_refusal = await check_project_scope(
        ctx.db, org_id=ctx.org_id, org_role=ctx.org_role, user_id=ctx.user_id, project_id=meeting.project_id,
    )
    if scope_refusal is not None:
        return ToolResult(False, scope_refusal)

    if params.status is not None and params.status not in _VALID_MEETING_STATUSES:
        return ToolResult(False, f"Meeting status must be one of: {', '.join(sorted(_VALID_MEETING_STATUSES))}.")
    if params.scheduled_at is None and params.status is None:
        return ToolResult(False, "I understood you want to update the meeting but couldn't determine what to change.")

    if params.scheduled_at is not None:
        meeting.scheduled_at = params.scheduled_at
    if params.status is not None:
        meeting.status = params.status
    await ctx.db.flush()

    await ctx.db.refresh(meeting)
    if params.scheduled_at is not None and meeting.scheduled_at != params.scheduled_at:
        return ToolResult(False, "Verification failed — the new time did not persist as expected.")
    if params.status is not None and meeting.status != params.status:
        return ToolResult(False, "Verification failed — the status did not persist as expected.")

    changes = []
    if params.scheduled_at is not None:
        changes.append(f"time={meeting.scheduled_at.strftime('%Y-%m-%d %H:%M')}")
    if params.status is not None:
        changes.append(f"status={meeting.status}")
    return ToolResult(
        True, f'Updated "{meeting.title}": {", ".join(changes)}.',
        actions=[ChatAction(type="meeting_updated", label=f'Meeting updated: "{meeting.title}"', payload={"meeting_id": meeting.id})],
        data={"meeting_id": meeting.id},
    )


register_tool(ToolSpec(
    name="schedule_meeting",
    description="Schedule a new meeting for a team and/or project.",
    input_schema=ScheduleMeetingInput,
    handler=_schedule_meeting_handler,
    allowed_roles=_MEETING_WRITE_ROLES,
))

register_tool(ToolSpec(
    name="update_meeting",
    description="Reschedule a meeting to a new time, cancel it, start it (in_progress), or end it (completed).",
    input_schema=UpdateMeetingInput,
    handler=_update_meeting_handler,
    allowed_roles=_MEETING_WRITE_ROLES,
))
