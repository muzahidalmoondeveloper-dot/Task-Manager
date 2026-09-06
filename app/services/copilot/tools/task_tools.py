"""Task domain tools (architecture Section 25.2, bounded to what this app's
chat currently supports). AUTO-tier tools only — CONFIRM-tier task actions
(reassign/delete/bulk-update/convert-request) are applied by
app.services.copilot.transaction.execute_confirmed_change_set, unchanged
from the accepted session fixes; this module does not touch that path,
it only registers their specs so check_write_authorized() has one place
to look up `allowed_roles` for every task tool, not just the AUTO ones."""

from app.core.org_roles import ADMIN, OWNER, PROJECT_MANAGER, TEAM_MANAGER
from app.models.task import Task
from app.repositories.task_repository import TaskRepository
from app.schemas.chat import ChatAction
from app.services.copilot.abac import check_project_scope
from app.services.copilot.tools.registry import ToolContext, ToolResult, ToolSpec, register_tool
from app.services.copilot.tools.schemas import (
    ConvertClientRequestInput,
    CreateTaskInput,
    DeleteTaskInput,
    ReassignTaskInput,
    UpdateTaskBulkInput,
    UpdateTaskFieldInput,
)

# Task mutation via chat is restricted to management roles — mirrors the
# product rule that already existed (informally) in ChatService._route()'s
# entry gate ("contact your team manager or admin to make task changes"),
# now enforced once, centrally, for every task tool including create_task
# (which previously had NO such check at all — see registry.py's module
# docstring for the exact bug this closes).
_TASK_WRITE_ROLES = frozenset({OWNER, ADMIN, TEAM_MANAGER, PROJECT_MANAGER})
# Delete/convert already had a stricter existing gate (no Project Manager)
# in chat_service.py's original handlers — preserved as-is here.
_TASK_DESTRUCTIVE_ROLES = frozenset({OWNER, ADMIN, TEAM_MANAGER})


async def _check_project_scope(ctx: ToolContext, project_id: int | None) -> str | None:
    """Thin wrapper around the shared abac.check_project_scope() — see that
    module's docstring for why this must be the SAME function transaction.py
    calls for the CONFIRM-tier tools, not a locally duplicated copy (that
    duplication is exactly how the CONFIRM-tier ABAC bypass happened)."""
    return await check_project_scope(
        ctx.db, org_id=ctx.org_id, org_role=ctx.org_role, user_id=ctx.user_id, project_id=project_id,
    )


async def _create_task_handler(ctx: ToolContext, params: CreateTaskInput) -> ToolResult:
    task_repo = TaskRepository(ctx.db, ctx.org_id)
    created: list[Task] = []

    # PARTIAL-FAILURE FIX (architecture item 16 — "partial-failure/
    # compensation handling"): TaskRepository.create() commits internally
    # per call. Calling it in this loop (the original version of this
    # handler did) meant a multi-task request that failed on item 3 of 5
    # left items 1-2 already permanently committed while run_tool()'s
    # except-block rollback (which only undoes *uncommitted* work) did
    # nothing to them, and the user was told "nothing was changed" — false.
    # Building the Task rows directly via the ORM and flushing once (never
    # committing here) makes the whole batch atomic: run_tool()'s
    # db.rollback() on any failure undoes every row in this batch, not just
    # the ones after whichever item failed. The eventual commit happens
    # exactly once, in audit.log_tool_execution(), only on full success.
    for item in params.tasks:
        scope_refusal = await _check_project_scope(ctx, item.project_id)
        if scope_refusal is not None:
            return ToolResult(False, scope_refusal)
        task = Task(
            name=item.name.strip(),
            description=item.description.strip() if item.description else None,
            start_date=item.start_date,
            due_date=item.due_date,
            assignee_id=item.assignee_id,
            project_id=item.project_id,
            team_id=item.team_id,
            status=item.status,
            created_by_id=ctx.user_id,
            organization_id=ctx.org_id,
        )
        ctx.db.add(task)
        created.append(task)

    await ctx.db.flush()

    # Post-execution verification (Section 30) — a flushed ORM object isn't
    # proof either; re-query every row and confirm it actually exists with
    # the values expected before calling this a success.
    verified: list[Task] = []
    for task in created:
        verify = await task_repo.get_by_id(task.id)
        if verify is None or verify.name != task.name:
            return ToolResult(
                False,
                f'Something went wrong creating "{task.name}" — it may not have been saved. Please check the Tasks page.',
            )
        verified.append(verify)
    created = verified

    if not created:
        return ToolResult(False, "I couldn't create any tasks from that.")

    names = ", ".join(f'"{t.name}"' for t in created)
    message = (
        f"Done! I created {len(created)} task{'s' if len(created) > 1 else ''}: {names}. "
        "You can view and manage them on the Tasks page."
    )
    return ToolResult(
        True, message,
        actions=[
            ChatAction(type="task_created", label=f'Task created: "{t.name}"', payload={"task_id": t.id, "task_name": t.name})
            for t in created
        ],
        data={"task_ids": [t.id for t in created]},
    )


async def _update_task_field_handler(ctx: ToolContext, params: UpdateTaskFieldInput) -> ToolResult:
    task_repo = TaskRepository(ctx.db, ctx.org_id)
    task = await task_repo.get_by_id(params.task_id)
    if task is None:
        return ToolResult(False, f"I couldn't find task #{params.task_id}.")

    scope_refusal = await _check_project_scope(ctx, task.project_id)
    if scope_refusal is not None:
        return ToolResult(False, scope_refusal)

    update_fields: dict = {}
    if params.name is not None:
        update_fields["name"] = params.name
    if params.status is not None:
        update_fields["status"] = params.status
    if params.due_date is not None:
        update_fields["due_date"] = params.due_date

    if not update_fields:
        return ToolResult(False, "I understood you want to update a task but couldn't determine what to change.")

    # Same atomicity fix as _create_task_handler above — TaskRepository.
    # update() commits internally, which would finalize the write before
    # this function's own post-write verification even ran, making a
    # verification failure's "Nothing else was changed" message false.
    # Direct setattr + a single flush (mirroring transaction.py's
    # _apply_task_update_fields) keeps the mutation inside the same
    # uncommitted transaction as its own verification.
    for field_name, value in update_fields.items():
        setattr(task, field_name, value)
    await ctx.db.flush()
    updated = task

    # Post-execution verification (Section 30).
    verify = await task_repo.get_by_id(updated.id)
    if verify is None:
        return ToolResult(False, "Something went wrong — the task no longer exists after the update.")
    for field_name, expected in update_fields.items():
        if getattr(verify, field_name) != expected:
            return ToolResult(False, f"Verification failed — {field_name} did not persist as expected. Nothing else was changed.")

    changes = ", ".join(f"{k}={v}" for k, v in update_fields.items())
    return ToolResult(
        True, f'Updated task "{verify.name}": {changes}.',
        actions=[ChatAction(type="task_updated", label=f'Task updated: "{verify.name}"', payload={"task_id": verify.id})],
        data={"task_id": verify.id},
    )


register_tool(ToolSpec(
    name="create_task",
    description=(
        "Create one or more new tasks. Use when the user explicitly asks to create/add task(s) "
        "with concrete names — never for analysis/summarization requests alone."
    ),
    input_schema=CreateTaskInput,
    handler=_create_task_handler,
    allowed_roles=_TASK_WRITE_ROLES,
))

register_tool(ToolSpec(
    name="update_task_field",
    description="Update a single task's name, status, or due date. Does not change the assignee (see reassign_task).",
    input_schema=UpdateTaskFieldInput,
    handler=_update_task_field_handler,
    allowed_roles=_TASK_WRITE_ROLES,
))

register_tool(ToolSpec(
    name="reassign_task",
    description="Change who a single task is assigned to. Always previewed and confirmed before applying.",
    input_schema=ReassignTaskInput,
    allowed_roles=_TASK_WRITE_ROLES,
))
register_tool(ToolSpec(
    name="delete_task_single",
    description="Permanently delete one task. Always previewed and confirmed before applying.",
    input_schema=DeleteTaskInput,
    allowed_roles=_TASK_DESTRUCTIVE_ROLES,
))
register_tool(ToolSpec(
    name="update_task_bulk",
    description="Update a field across many tasks at once. Always previewed and confirmed before applying.",
    input_schema=UpdateTaskBulkInput,
    allowed_roles=_TASK_WRITE_ROLES,
))
register_tool(ToolSpec(
    name="convert_client_request_to_task",
    description="Convert a pending client task request into a real task. Always previewed and confirmed before applying.",
    input_schema=ConvertClientRequestInput,
    allowed_roles=_TASK_DESTRUCTIVE_ROLES,
))
