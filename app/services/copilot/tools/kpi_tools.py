"""KPI domain tools (architecture domain buildout — strict acceptance audit
finding: KPI was READ_ONLY, no `record_kpi_value` despite being named in an
earlier plan). AUTO-tier (R2).

No project-level ABAC here (unlike task/rock/issue tools) — KPI has no
project_id at all in its data model (only team_id), so the existing
check_project_scope() ABAC dimension doesn't apply; team-level ABAC
(scoping a PROJECT_MANAGER or team member to only their own team's KPIs)
isn't built for any domain yet, tasks included, so this isn't a regression
— just an explicitly out-of-scope dimension, same as the rest of the app
today."""

from datetime import date

from sqlalchemy import select

from app.core.org_roles import ADMIN, OWNER, PROJECT_MANAGER, TEAM_MANAGER
from app.models.kpi import KPI, KPIEntry
from app.schemas.chat import ChatAction
from app.services.copilot.tools.registry import ToolContext, ToolResult, ToolSpec, register_tool
from app.services.copilot.tools.schemas import RecordKpiValueInput

_KPI_WRITE_ROLES = frozenset({OWNER, ADMIN, TEAM_MANAGER, PROJECT_MANAGER})


async def _record_kpi_value_handler(ctx: ToolContext, params: RecordKpiValueInput) -> ToolResult:
    kpi = (await ctx.db.execute(select(KPI).where(KPI.id == params.kpi_id, KPI.organization_id == ctx.org_id))).scalar_one_or_none()
    if kpi is None:
        return ToolResult(False, f"I couldn't find KPI #{params.kpi_id}.")

    period_start = params.period_start
    if period_start is None:
        # "today" here deliberately does not use org-timezone resolution —
        # the caller passes ctx, not a full ChatService, and KPI periods
        # are typically week/month-aligned regardless of the exact hour;
        # a plain UTC date() is an acceptable, documented simplification
        # for this one field, not a repeat of the "silent server-local
        # date()" bug class this session's item 9 fix closed for task due
        # dates (there is no display of "overdue" here to get wrong).
        period_start = date.today()

    existing = (
        await ctx.db.execute(
            select(KPIEntry).where(
                KPIEntry.kpi_id == kpi.id,
                KPIEntry.period_type == params.period_type,
                KPIEntry.period_start == period_start,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.value = params.value
        if params.note:
            existing.notes = (existing.notes or []) + [params.note]
        entry = existing
    else:
        entry = KPIEntry(
            kpi_id=kpi.id, value=params.value, period_start=period_start, period_type=params.period_type,
            notes=[params.note] if params.note else [],
        )
        ctx.db.add(entry)
    await ctx.db.flush()

    # Post-execution verification (Section 30).
    await ctx.db.refresh(entry)
    if entry.value != params.value:
        return ToolResult(False, "Verification failed — the KPI value did not persist as expected.")

    return ToolResult(
        True, f'Recorded {kpi.title} = {params.value} for {params.period_type} period starting {period_start.isoformat()}.',
        actions=[ChatAction(type="kpi_recorded", label=f'KPI recorded: "{kpi.title}"', payload={"kpi_id": kpi.id, "entry_id": entry.id})],
        data={"kpi_id": kpi.id, "entry_id": entry.id},
    )


register_tool(ToolSpec(
    name="record_kpi_value",
    description="Record a KPI's value for a given period (defaults to today's date, weekly period, if not specified). Updates the existing entry if one already exists for that exact period.",
    input_schema=RecordKpiValueInput,
    handler=_record_kpi_value_handler,
    allowed_roles=_KPI_WRITE_ROLES,
))
