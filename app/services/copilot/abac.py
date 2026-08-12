"""Shared ABAC (attribute-based access control) checks (architecture item 2
/ security gap #1 from the strict acceptance audit) — the single place a
resource-scope decision like "is this project manager actually a member of
the project this task belongs to" is made, so every execution path for a
given tool (AUTO-tier via tools/task_tools.py, CONFIRM-tier via
transaction.py) enforces the identical rule instead of two independently
maintained copies that can drift apart.

BUG THIS CLOSES: `_check_project_scope` originally lived only in
task_tools.py and was called only by the AUTO-tier `create_task`/
`update_task_field` handlers. The CONFIRM-tier tools (`reassign_task`,
`update_task_bulk` — both of which list PROJECT_MANAGER in their
allowed_roles) executed through transaction.py, which never called it —
a PROJECT_MANAGER could reassign or bulk-update tasks outside every
project they actually manage, simply by going through the confirm flow
instead of the instant-apply one.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.org_roles import PROJECT_MANAGER
from app.repositories.project_repository import ProjectRepository


async def check_project_scope(
    db: AsyncSession, *, org_id: uuid.UUID, org_role: str, user_id: int, project_id: int | None,
) -> str | None:
    """Returns a user-facing refusal message if `user_id` (a PROJECT_MANAGER)
    is not a member of `project_id`, or None if the action is in-scope.

    Only PROJECT_MANAGER is resource-scoped — org/admin/team_manager roles
    are unrestricted org-wide by existing product rule (they already see
    "all tasks" in list/update/delete handlers). A no-op for every other
    role, including PROJECT_MANAGER acting on a task that isn't attached to
    any project at all in the AUTO-tier sense (project_id is None) is
    treated as out-of-scope too — a PM must be able to name which project
    a task belongs to, not implicitly touch unscoped tasks.
    """
    if org_role != PROJECT_MANAGER:
        return None
    if project_id is None:
        return "As a project manager, I can only act on tasks that belong to one of your projects."
    project_repo = ProjectRepository(db, org_id)
    if not await project_repo.is_member(project_id, user_id):
        return "You don't manage that project, so I can't make changes to its tasks through the assistant."
    return None
