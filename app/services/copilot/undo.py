"""Undo System (spec Section 46) — only for actions whose inverse is
unambiguous: reassigning a task back to its previous owner, or reverting a
bulk field update to each task's previous value. Deletion is never undoable
here (the rows are actually gone), matching the spec's own acknowledgement
that not every action is reversible."""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.copilot import AIOperation
from app.models.task import Task
from app.repositories.task_repository import TaskRepository

UNDO_TTL_MINUTES = 10


async def register_undo(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    session_id: int,
    user_id: int,
    tool_name: str,
    inverse: dict,
    description: str,
) -> AIOperation:
    operation = AIOperation(
        organization_id=org_id,
        session_id=session_id,
        created_by_id=user_id,
        tool_name=tool_name,
        inverse_json=inverse,
        description=description,
        reversible=True,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=UNDO_TTL_MINUTES),
    )
    db.add(operation)
    await db.flush()
    return operation


async def get_operation(db: AsyncSession, org_id: uuid.UUID, operation_id: int) -> AIOperation | None:
    result = await db.execute(
        select(AIOperation).where(
            AIOperation.id == operation_id,
            AIOperation.organization_id == org_id,
        )
    )
    return result.scalar_one_or_none()


def is_expired(operation: AIOperation) -> bool:
    expires_at = operation.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at < datetime.now(timezone.utc)


async def execute_undo(db: AsyncSession, org_id: uuid.UUID, operation: AIOperation) -> str:
    """Applies the stored inverse. Returns a human-readable result message.

    Mutates Task columns directly (setattr) rather than via
    TaskRepository.update() — that method commits internally, which would
    break atomicity for the bulk-revert loop below (a failure partway
    through would leave some tasks reverted and others not, silently
    committed one row at a time). Everything here lands in exactly one
    commit, with a rollback if anything goes wrong before that point."""
    task_repo = TaskRepository(db, org_id)
    inverse = operation.inverse_json

    try:
        if operation.tool_name == "reassign_task":
            task = await task_repo.get_by_id(inverse["task_id"])
            if task is None:
                operation.undone = True
                await db.commit()
                return "That task no longer exists — nothing to undo."
            task.assignee_id = inverse["previous_assignee_id"]
            operation.undone = True
            await db.commit()
            return f'Reverted "{task.name}" back to its previous assignee.'

        if operation.tool_name == "reassign_team_manager":
            from app.models.team import Team

            team = (await db.execute(select(Team).where(Team.id == inverse["team_id"]))).scalar_one_or_none()
            if team is None:
                operation.undone = True
                await db.commit()
                return "That team no longer exists — nothing to undo."
            team.team_manager_id = inverse["previous_manager_id"]
            operation.undone = True
            await db.commit()
            return f'Reverted team "{team.name}" back to its previous manager.'

        if operation.tool_name == "update_task_bulk":
            reverted = 0
            for task_id, old_values in inverse.get("per_task", {}).items():
                result = await db.execute(select(Task).where(Task.id == int(task_id)))
                task = result.scalar_one_or_none()
                if task is None:
                    continue
                for field, value in old_values.items():
                    setattr(task, field, value)
                reverted += 1
            operation.undone = True
            await db.commit()
            return f"Reverted {reverted} task(s) back to their previous values."
    except Exception:
        await db.rollback()
        raise

    operation.undone = True
    await db.commit()
    return "This action type has no automatic undo — no changes were made."
