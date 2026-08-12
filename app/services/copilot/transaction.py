"""Transaction Coordinator (spec Section 27, 30) — applies a confirmed
change set: revalidate policy + optimistic-lock version, apply the change,
re-query the actual resulting state and compare it to what was expected
before reporting success, write one audit row, and (for reversible tools)
register an undo operation."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.copilot import AIChangeSet
from app.models.task import Task
from app.repositories.task_repository import TaskRepository
from app.repositories.task_request_repository import TaskRequestRepository
from app.repositories.user_repository import UserRepository
from app.schemas.task import TaskUpdate
from app.services.copilot import audit, change_sets, undo
from app.services.copilot.abac import check_project_scope
from app.services.copilot.policy import check_tool_policy
from app.services.copilot.risk import risk_level

# CONFIRM-tier tools whose allowed_roles include PROJECT_MANAGER (see
# tools/task_tools.py's _TASK_WRITE_ROLES) and therefore need the same
# per-task ABAC scoping the AUTO-tier tools already enforce (security gap
# #1 from the strict acceptance audit — see abac.py's module docstring for
# the exact bypass this closes). delete_task_single/convert_client_request
# use the destructive-only role set, which never includes PROJECT_MANAGER,
# so they need no additional scoping here.
_PROJECT_SCOPED_CONFIRM_TOOLS = {"reassign_task", "update_task_bulk"}


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
    # Captured up front — after a rollback (see the except block below),
    # SQLAlchemy expires every attribute on every object in the session
    # regardless of expire_on_commit, so change_set.session_id/created_by_id
    # would trigger an implicit (illegal, in async mode) reload if read
    # after that point. Plain local variables sidestep it entirely.
    session_id = change_set.session_id
    created_by_id = change_set.created_by_id

    # 1. Re-validate authorization — the confirming user's role may have
    #    changed since the change set was previewed.
    decision = check_tool_policy(org_role=org_role, tool_name=tool_name)
    if decision == "deny":
        change_set.status = "cancelled"
        await change_sets.clear_session_state(db, session_id)
        await db.commit()
        return TransactionResult(False, "You no longer have permission to complete this action.")

    # 2. Re-validate the change set hasn't gone stale (any affected task
    #    changed since the preview was shown).
    if not await change_sets.check_versions_fresh(db, change_set):
        change_set.status = "stale"
        await change_sets.clear_session_state(db, session_id)
        await db.commit()
        await audit.log_tool_execution(
            db, org_id=org_id, session_id=session_id, user_id=created_by_id,
            tool_name=tool_name, risk_level=risk_level(tool_name), policy_decision="stale",
            params=params, result_summary="Rejected — affected record(s) changed since preview.", success=False,
            trace_id=trace_id,
        )
        return TransactionResult(
            False,
            "This changed since you reviewed it — please ask again so I can show you a fresh preview.",
        )

    task_repo = TaskRepository(db, org_id)

    # 2.5. ABAC re-validation (security gap #1 — see _PROJECT_SCOPED_CONFIRM_TOOLS
    #    and abac.py's module docstring). Re-checked here, at confirm time, for
    #    the same reason step 1 re-checks RBAC: the confirming user's project
    #    memberships may have changed since the change set was previewed, and
    #    this is the only point every CONFIRM-tier execution path funnels
    #    through regardless of which chat_service.py handler built the change
    #    set — closing the gap here closes it for all of them at once.
    if tool_name in _PROJECT_SCOPED_CONFIRM_TOOLS:
        scoped_task_ids = [params["task_id"]] if tool_name == "reassign_task" else params["task_ids"]
        for scoped_task_id in scoped_task_ids:
            scoped_task = await task_repo.get_by_id(scoped_task_id)
            if scoped_task is None:
                continue  # surfaced as "no longer exists" by the apply helper itself
            refusal = await check_project_scope(
                db, org_id=org_id, org_role=org_role, user_id=created_by_id, project_id=scoped_task.project_id,
            )
            if refusal is not None:
                change_set.status = "cancelled"
                await change_sets.clear_session_state(db, session_id)
                await db.commit()
                await audit.log_tool_execution(
                    db, org_id=org_id, session_id=session_id, user_id=created_by_id,
                    tool_name=tool_name, risk_level=risk_level(tool_name), policy_decision="abac_deny",
                    params=params, result_summary=f"ABAC refused: {refusal}", success=False,
                    trace_id=trace_id,
                )
                return TransactionResult(False, refusal)

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
        elif tool_name == "reassign_team_manager":
            message = await _apply_reassign_team_manager(db, org_id, params)
        else:
            return TransactionResult(False, f"Unknown action type: {tool_name}")
    except Exception as exc:
        # CRITICAL: roll back BEFORE any further DB activity. The apply
        # helpers above only flush() (never commit()), specifically so a
        # failure here can be fully undone — but audit.log_tool_execution()
        # calls db.commit() internally, and this session's pending flushed
        # changes would ride along with that commit if we didn't roll back
        # first. Without this, a bulk update that fails on item 3 of 5
        # would silently persist items 1-2 while telling the user "nothing
        # was modified" (architecture Section 27; master-prompt Section 4.4).
        await db.rollback()
        await audit.log_tool_execution(
            db, org_id=org_id, session_id=session_id, user_id=created_by_id,
            tool_name=tool_name, risk_level=risk_level(tool_name), policy_decision="allow",
            params=params, result_summary=f"Execution failed: {exc}", success=False,
            trace_id=trace_id,
        )
        return TransactionResult(False, "Something went wrong applying that change. Nothing was modified.")

    # Reversible tools register an undo operation with the inverse captured
    # during application (see helpers below, via change_set.params_json's
    # "_inverse" scratch key set by the apply helpers). Still inside the
    # same uncommitted transaction as the mutation itself (register_undo
    # only flushes) — the change_set status update below performs the one
    # commit that covers the mutation, the undo row, and the status change
    # together, atomically.
    inverse = params.pop("_inverse", None)
    if inverse is not None:
        op = await undo.register_undo(
            db, org_id=org_id, session_id=session_id, user_id=created_by_id,
            tool_name=tool_name, inverse=inverse, description=message,
        )
        operation_id = op.id

    change_set.status = "executed"
    await change_sets.clear_session_state(db, session_id)
    await db.commit()

    await audit.log_tool_execution(
        db, org_id=org_id, session_id=session_id, user_id=created_by_id,
        tool_name=tool_name, risk_level=risk_level(tool_name), policy_decision="allow",
        params=params, result_summary=message, success=True, trace_id=trace_id,
    )
    return TransactionResult(True, message, operation_id)


def _apply_task_update_fields(task, updates: dict) -> None:
    """Mutate a Task's columns directly (setattr), matching what
    TaskRepository.update() would set — but WITHOUT its internal
    db.commit(). Used inside the transaction coordinator so every mutation
    in a change set lands in the same atomic transaction (architecture
    Section 27 / master-prompt Section 4.4); TaskRepository itself is left
    unchanged since it's shared by the rest of the application outside the
    copilot's transaction boundary."""
    data = TaskUpdate(**updates).model_dump(exclude_unset=True)
    if "name" in data and data["name"]:
        data["name"] = data["name"].strip()
    for key, value in data.items():
        setattr(task, key, value)


async def _apply_reassign_task(db, org_id, change_set, task_repo: TaskRepository, params: dict) -> str:
    task = await task_repo.get_by_id(params["task_id"])
    if task is None:
        raise ValueError("Task no longer exists.")
    updates: dict = params["updates"]
    new_assignee_id = updates["assignee_id"]
    previous_assignee_id = task.assignee_id
    _apply_task_update_fields(task, updates)
    await db.flush()

    # Post-execution verification (Section 30) — re-query and compare
    # against the flushed-but-not-yet-committed state.
    await db.refresh(task)
    if task.assignee_id != new_assignee_id:
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
    task_id = task.id
    await db.delete(task)
    await db.flush()

    verify = await task_repo.get_by_id(task_id)
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

    task = Task(
        name=request.title.strip(),
        status="todo",
        project_id=request.project_id,
        organization_id=org_id,
        created_by_id=change_set.created_by_id,
    )
    db.add(task)
    await db.flush()
    await request_repo.mark_converted(request, task.id, reviewed_by_id=change_set.created_by_id)
    await db.flush()

    # Post-execution verification (Section 30) — re-query and compare.
    verify = await request_repo.get_by_id(request.id)
    if verify is None or verify.status != "converted" or verify.converted_task_id != task.id:
        raise ValueError("Verification failed — the request conversion did not persist as expected.")

    return f'Converted request "{request.title}" into task "{task.name}" (#{task.id}).'


async def _apply_reassign_team_manager(db, org_id, params: dict) -> str:
    """architecture item 2 — Teams lifecycle. CONFIRM-tier (R3): changing
    who leads a team is a personnel/leadership change with wider blast
    radius (accountability for the whole team's task/rock/KPI workload)
    than a single-field rename, so it goes through preview+confirm exactly
    like reassign_task does, rather than update_team's AUTO tier."""
    from app.models.team import Team

    team = (await db.execute(select(Team).where(Team.id == params["team_id"], Team.organization_id == org_id))).scalar_one_or_none()
    if team is None:
        raise ValueError("That team no longer exists.")

    new_manager_id = params["new_manager_id"]
    new_manager = await UserRepository(db).get_by_id(new_manager_id)
    if new_manager is None:
        raise ValueError("That user no longer exists.")

    previous_manager_id = team.team_manager_id
    team.team_manager_id = new_manager_id
    await db.flush()

    await db.refresh(team)
    if team.team_manager_id != new_manager_id:
        raise ValueError("Verification failed — the manager reassignment did not persist as expected.")

    params["_inverse"] = {"team_id": team.id, "previous_manager_id": previous_manager_id}
    return f'Reassigned team "{team.name}" to be managed by {new_manager.full_name}.'


async def _apply_update_task_bulk(db, org_id, change_set, task_repo: TaskRepository, params: dict) -> str:
    task_ids: list[int] = params["task_ids"]
    updates: dict = params["updates"]
    per_task_old: dict[str, dict] = {}
    touched_tasks = []
    for task_id in task_ids:
        task = await task_repo.get_by_id(task_id)
        if task is None:
            continue
        per_task_old[str(task_id)] = {k: getattr(task, k) for k in updates}
        _apply_task_update_fields(task, updates)
        touched_tasks.append(task)
    await db.flush()

    # Post-execution verification (Section 30) — every touched task must
    # actually reflect the new values before this is reported as applied;
    # a single mismatch fails the whole bulk operation (caught by the
    # caller's try/except, which rolls back rather than committing a
    # partially-wrong batch and calling it done).
    for task in touched_tasks:
        await db.refresh(task)
        for field, expected in TaskUpdate(**updates).model_dump(exclude_unset=True).items():
            if getattr(task, field) != expected:
                raise ValueError(f"Verification failed — task {task.id} did not persist {field}={expected!r}.")

    changes = ", ".join(f"{k}={v}" for k, v in updates.items())
    params["_inverse"] = {"per_task": per_task_old}
    return f"Updated {len(touched_tasks)} task(s): {changes}."
