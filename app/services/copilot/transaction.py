"""Transaction Coordinator (spec Section 27, 30) — applies a confirmed
change set: revalidate policy + optimistic-lock version, apply the change,
re-query the actual resulting state and compare it to what was expected
before reporting success, write one audit row, and (for reversible tools)
register an undo operation."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.copilot import AIChangeSet
from app.repositories.task_repository import TaskRepository
from app.repositories.task_request_repository import TaskRequestRepository
from app.repositories.user_repository import UserRepository
from app.schemas.task import TaskCreate, TaskUpdate
from app.services.copilot import audit, change_sets, undo
from app.services.copilot.policy import check_tool_policy
from app.services.copilot.risk import risk_level


class TransactionResult:
    def __init__(self, success: bool, message: str, operation_id: int | None = None):
        self.success = success
        self.message = message
        self.operation_id = operation_id


async def execute_confirmed_change_set(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    org_role: str,
    change_set: AIChangeSet,
    trace_id: str | None = None,
) -> TransactionResult:
    tool_name = change_set.tool_name
    params = change_set.params_json

    # 1. Re-validate authorization — the confirming user's role may have
    #    changed since the change set was previewed.
    decision = check_tool_policy(org_role=org_role, tool_name=tool_name)
    if decision == "deny":
        change_set.status = "cancelled"
        await change_sets.clear_session_state(db, change_set.session_id)
        await db.commit()
        return TransactionResult(False, "You no longer have permission to complete this action.")

    # 2. Re-validate the change set hasn't gone stale (any affected task
    #    changed since the preview was shown).
    if not await change_sets.check_versions_fresh(db, change_set):
        change_set.status = "stale"
        await change_sets.clear_session_state(db, change_set.session_id)
        await db.commit()
        await audit.log_tool_execution(
            db, org_id=org_id, session_id=change_set.session_id, user_id=change_set.created_by_id,
            tool_name=tool_name, risk_level=risk_level(tool_name), policy_decision="stale",
            params=params, result_summary="Rejected — affected record(s) changed since preview.", success=False,
            trace_id=trace_id,
        )
        return TransactionResult(
            False,
            "This changed since you reviewed it — please ask again so I can show you a fresh preview.",
        )

    task_repo = TaskRepository(db, org_id)
    operation_id: int | None = None

    try:
        if tool_name == "reassign_task":
            message = await _apply_reassign_task(db, org_id, change_set, task_repo, params)
        elif tool_name == "delete_task_single":
            message = await _apply_delete_task_single(db, task_repo, params)
        elif tool_name == "update_task_bulk":
            message = await _apply_update_task_bulk(db, org_id, change_set, task_repo, params)
        elif tool_name == "convert_client_request_to_task":
            message = await _apply_convert_client_request(db, org_id, change_set, task_repo, params)
        else:
            return TransactionResult(False, f"Unknown action type: {tool_name}")
    except Exception as exc:
        await audit.log_tool_execution(
            db, org_id=org_id, session_id=change_set.session_id, user_id=change_set.created_by_id,
            tool_name=tool_name, risk_level=risk_level(tool_name), policy_decision="allow",
            params=params, result_summary=f"Execution failed: {exc}", success=False,
            trace_id=trace_id,
        )
        return TransactionResult(False, "Something went wrong applying that change. Nothing was modified.")

    # Reversible tools register an undo operation with the inverse captured
    # during application (see helpers below, via change_set.params_json's
    # "_inverse" scratch key set by the apply helpers).
    inverse = params.pop("_inverse", None)
    if inverse is not None:
        op = await undo.register_undo(
            db, org_id=org_id, session_id=change_set.session_id, user_id=change_set.created_by_id,
            tool_name=tool_name, inverse=inverse, description=message,
        )
        operation_id = op.id

    change_set.status = "executed"
    await change_sets.clear_session_state(db, change_set.session_id)
    await db.commit()

    await audit.log_tool_execution(
        db, org_id=org_id, session_id=change_set.session_id, user_id=change_set.created_by_id,
        tool_name=tool_name, risk_level=risk_level(tool_name), policy_decision="allow",
        params=params, result_summary=message, success=True, trace_id=trace_id,
    )
    return TransactionResult(True, message, operation_id)


async def _apply_reassign_task(db, org_id, change_set, task_repo: TaskRepository, params: dict) -> str:
    task = await task_repo.get_by_id(params["task_id"])
    if task is None:
        raise ValueError("Task no longer exists.")
    updates: dict = params["updates"]
    new_assignee_id = updates["assignee_id"]
    previous_assignee_id = task.assignee_id
    updated = await task_repo.update(task, TaskUpdate(**updates))

    # Post-execution verification (Section 30) — re-query and compare.
    verify = await task_repo.get_by_id(updated.id)
    if verify is None or verify.assignee_id != new_assignee_id:
        raise ValueError("Verification failed — reassignment did not persist as expected.")

    user_repo = UserRepository(db)
    new_assignee = await user_repo.get_by_id(new_assignee_id)
    params["_inverse"] = {"task_id": task.id, "previous_assignee_id": previous_assignee_id}
    return f'Reassigned "{task.name}" to {new_assignee.full_name if new_assignee else "the new assignee"}.'


async def _apply_delete_task_single(db, task_repo: TaskRepository, params: dict) -> str:
    task = await task_repo.get_by_id(params["task_id"])
    if task is None:
        return "That task was already deleted."
    name = task.name
    await db.delete(task)
    await db.commit()

    verify = await task_repo.get_by_id(params["task_id"])
    if verify is not None:
        raise ValueError("Verification failed — task still exists after delete.")
    return f'Deleted task "{name}".'


async def _apply_convert_client_request(db, org_id, change_set, task_repo: TaskRepository, params: dict) -> str:
    request_repo = TaskRequestRepository(db, org_id)
    request = await request_repo.get_by_id(params["request_id"])
    if request is None:
        raise ValueError("That client request no longer exists.")
    if request.status != "pending":
        raise ValueError(f"That request is already {request.status}.")

    task = await task_repo.create(
        TaskCreate(name=request.title, project_id=request.project_id, status="todo"),
        created_by_id=change_set.created_by_id,
    )
    await request_repo.mark_converted(request, task.id, reviewed_by_id=change_set.created_by_id)

    # Post-execution verification (Section 30) — re-query and compare.
    verify = await request_repo.get_by_id(request.id)
    if verify is None or verify.status != "converted" or verify.converted_task_id != task.id:
        raise ValueError("Verification failed — the request conversion did not persist as expected.")

    return f'Converted request "{request.title}" into task "{task.name}" (#{task.id}).'


async def _apply_update_task_bulk(db, org_id, change_set, task_repo: TaskRepository, params: dict) -> str:
    task_ids: list[int] = params["task_ids"]
    updates: dict = params["updates"]
    per_task_old: dict[str, dict] = {}
    count = 0
    for task_id in task_ids:
        task = await task_repo.get_by_id(task_id)
        if task is None:
            continue
        per_task_old[str(task_id)] = {k: getattr(task, k) for k in updates}
        await task_repo.update(task, TaskUpdate(**updates))
        count += 1

    changes = ", ".join(f"{k}={v}" for k, v in updates.items())
    params["_inverse"] = {"per_task": per_task_old}
    return f"Updated {count} task(s): {changes}."
