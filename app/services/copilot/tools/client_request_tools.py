"""Client Request domain tools (architecture domain buildout — strict
acceptance audit finding: Client Requests had no chat-driven submission
path, only staff-side conversion). AUTO-tier (R2).

`submit_client_request` is the one deliberate exception to "clients have no
write tools" (see policy.py's _CLIENT_ALLOWED_WRITE_TOOLS) — a client
submitting a new request for their own project through chat is the same
action the regular UI form already lets them do."""

from sqlalchemy import select

from app.core.org_roles import CLIENT
from app.models.task_request import TaskRequest
from app.repositories.project_repository import ProjectRepository
from app.schemas.chat import ChatAction
from app.services.copilot.tools.registry import ToolContext, ToolResult, ToolSpec, register_tool
from app.services.copilot.tools.schemas import SubmitClientRequestInput

_CLIENT_REQUEST_WRITE_ROLES = frozenset({CLIENT})


async def _submit_client_request_handler(ctx: ToolContext, params: SubmitClientRequestInput) -> ToolResult:
    # ABAC: a client may only submit a request for a project they're
    # actually a member of — this is the client-side mirror of
    # _check_project_scope's PROJECT_MANAGER scoping in task_tools.py.
    project_repo = ProjectRepository(ctx.db, ctx.org_id)
    if not await project_repo.is_member(params.project_id, ctx.user_id):
        return ToolResult(False, "You can only submit requests for a project you're a member of.")

    request = TaskRequest(
        organization_id=ctx.org_id,
        project_id=params.project_id,
        submitted_by_id=ctx.user_id,
        title=params.title.strip(),
        description=params.description,
        status="pending",
    )
    ctx.db.add(request)
    await ctx.db.flush()

    verify = (await ctx.db.execute(select(TaskRequest).where(TaskRequest.id == request.id))).scalar_one_or_none()
    if verify is None or verify.title != params.title.strip():
        return ToolResult(False, f'Something went wrong submitting "{params.title}" — please check the Requests page.')

    return ToolResult(
        True, f'Submitted request "{verify.title}". Your project manager will review it.',
        actions=[ChatAction(type="client_request_submitted", label=f'Request submitted: "{verify.title}"', payload={"request_id": verify.id})],
        data={"request_id": verify.id},
    )


register_tool(ToolSpec(
    name="submit_client_request",
    description="Submit a new task request for a project (client-only — staff create tasks directly instead).",
    input_schema=SubmitClientRequestInput,
    handler=_submit_client_request_handler,
    allowed_roles=_CLIENT_REQUEST_WRITE_ROLES,
))
