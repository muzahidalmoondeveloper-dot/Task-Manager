"""Reporting domain tools (architecture item 3 — "complete Reporting/
Knowledge and Scoreboard capabilities required by the architecture"). Prior
to this module, the only chat-facing "reporting" capability was
knowledge_tools.py's search_everything/search_knowledge (retrieval side) —
there was no way to actually GENERATE a project status report through
chat, even though the app already has a full report-generation engine
(app/services/report_generation_service.py) driving the HTTP Reports
module. This wraps that SAME engine — not a separate, chat-only
re-implementation — so a chat-generated report is identical in content and
structure to one made through the UI.

AUTO-tier (R2): one Report row, visible/deletable from the existing Reports
page if wrong, same risk shape as every other domain's single-record
create."""

from sqlalchemy import select

from app.core.org_roles import ADMIN, OWNER, TEAM_MANAGER
from app.models.project import Project
from app.schemas.chat import ChatAction
from app.schemas.report import ReportCreate
from app.services.copilot.abac import check_project_scope
from app.services.copilot.tools.registry import ToolContext, ToolResult, ToolSpec, register_tool
from app.services.copilot.tools.schemas import GenerateProjectReportInput

# Mirrors app.core.tenant.require_org_manager exactly (the same dependency
# gating POST /reports in the HTTP API) — not a separately-invented role set.
_REPORT_WRITE_ROLES = frozenset({OWNER, ADMIN, TEAM_MANAGER})


async def _generate_project_report_handler(ctx: ToolContext, params: GenerateProjectReportInput) -> ToolResult:
    from app.repositories.project_repository import ProjectRepository
    from app.repositories.report_repository import ReportRepository
    from app.services.report_generation_service import ReportGenerationService

    project_repo = ProjectRepository(ctx.db, ctx.org_id)
    project = await project_repo.get_by_id(params.project_id)
    if project is None:
        return ToolResult(False, f"I couldn't find project #{params.project_id}.")

    scope_refusal = await check_project_scope(
        ctx.db, org_id=ctx.org_id, org_role=ctx.org_role, user_id=ctx.user_id, project_id=project.id,
    )
    if scope_refusal is not None:
        return ToolResult(False, scope_refusal)

    report_repo = ReportRepository(ctx.db, ctx.org_id)
    payload = ReportCreate(project_id=params.project_id, report_type=params.report_type, title=params.title.strip())
    report = await report_repo.create(payload, created_by_id=ctx.user_id)

    generation = ReportGenerationService(ctx.db, ctx.org_id)
    await generation.generate(report)
    await ctx.db.flush()

    verify = (await ctx.db.execute(select(Project).where(Project.id == params.project_id))).scalar_one_or_none()
    if verify is None:
        return ToolResult(False, "Something went wrong generating the report — please check the Reports page.")

    return ToolResult(
        True, f'Generated {params.report_type} report "{report.title}" for project "{project.name}".',
        actions=[ChatAction(type="report_generated", label=f'Report generated: "{report.title}"', payload={"report_id": report.id})],
        data={"report_id": report.id},
    )


register_tool(ToolSpec(
    name="generate_project_report",
    description="Generate a status report (weekly/monthly/client/team_performance) for a project, populated from its current Rocks, KPIs, Tasks, Risks, and Issues.",
    input_schema=GenerateProjectReportInput,
    handler=_generate_project_report_handler,
    allowed_roles=_REPORT_WRITE_ROLES,
))
