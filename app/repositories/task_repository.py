import uuid
from datetime import date

from sqlalchemy import false, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.task_assignment import validate_task_assignee
from app.models.task import Task
from app.repositories.base_tenant_repository import TenantRepository
from app.schemas.task import TaskCreate, TaskUpdate

_TASK_EAGER = [
    selectinload(Task.assignee),
    selectinload(Task.project),
    selectinload(Task.team),
]


class TaskRepository(TenantRepository):
    def __init__(self, db: AsyncSession, org_id: uuid.UUID) -> None:
        super().__init__(db, org_id)

    def _base_stmt(self):
        return select(Task).where(Task.organization_id == self.org_id).options(*_TASK_EAGER)

    async def list_all(
        self,
        *,
        status: str | None = None,
        priority: str | None = None,
        project_id: int | None = None,
        team_id: int | None = None,
        assignee_id: int | None = None,
        due_date_from: date | None = None,
        due_date_to: date | None = None,
        overdue: bool = False,
        scope_team_ids: set[int] | None = None,
        scope_project_ids: set[int] | None = None,
    ) -> list[Task]:
        """`scope_team_ids`/`scope_project_ids` are the Team Manager
        Task-scope follow-up's org-wide-listing guard: `None` (the
        default, used for Owner/Admin) means unrestricted — every other
        filter below still applies, but no team/project boundary is
        added. Passing either as a set (even an empty one, for a manager
        of zero teams) means the caller is scope-restricted: only tasks
        whose team_id is in scope_team_ids OR whose project_id is in
        scope_project_ids are returned, and a scoped caller who matches
        neither set at all sees nothing — never silently falls back to
        the full organization."""
        stmt = self._base_stmt()
        if status:
            stmt = stmt.where(Task.status == status)
        if priority:
            stmt = stmt.where(Task.priority == priority)
        if project_id:
            stmt = stmt.where(Task.project_id == project_id)
        if team_id:
            stmt = stmt.where(Task.team_id == team_id)
        if assignee_id:
            stmt = stmt.where(Task.assignee_id == assignee_id)
        if due_date_from:
            stmt = stmt.where(Task.due_date >= due_date_from)
        if due_date_to:
            stmt = stmt.where(Task.due_date <= due_date_to)
        if overdue:
            today = date.today()
            stmt = stmt.where(Task.due_date < today).where(
                Task.status.notin_(["done", "pending_review"])
            )
        if scope_team_ids is not None or scope_project_ids is not None:
            conditions = []
            if scope_team_ids:
                conditions.append(Task.team_id.in_(scope_team_ids))
            if scope_project_ids:
                conditions.append(Task.project_id.in_(scope_project_ids))
            stmt = stmt.where(or_(*conditions) if conditions else false())
        stmt = stmt.order_by(Task.due_date.asc(), Task.created_at.desc())
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def list_for_assignee(
        self,
        user_id: int,
        *,
        status: str | None = None,
        priority: str | None = None,
        project_id: int | None = None,
        team_id: int | None = None,
        due_date_from: date | None = None,
        due_date_to: date | None = None,
        overdue: bool = False,
    ) -> list[Task]:
        stmt = self._base_stmt().where(Task.assignee_id == user_id)
        if status:
            stmt = stmt.where(Task.status == status)
        if priority:
            stmt = stmt.where(Task.priority == priority)
        if project_id:
            stmt = stmt.where(Task.project_id == project_id)
        if team_id:
            stmt = stmt.where(Task.team_id == team_id)
        if due_date_from:
            stmt = stmt.where(Task.due_date >= due_date_from)
        if due_date_to:
            stmt = stmt.where(Task.due_date <= due_date_to)
        if overdue:
            today = date.today()
            stmt = stmt.where(Task.due_date < today).where(
                Task.status.notin_(["done", "pending_review"])
            )
        stmt = stmt.order_by(Task.due_date.asc(), Task.created_at.desc())
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def list_by_project(self, project_id: int) -> list[Task]:
        stmt = (
            self._base_stmt()
            .where(Task.project_id == project_id)
            .order_by(Task.due_date.asc(), Task.created_at.desc())
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def list_by_team(self, team_id: int) -> list[Task]:
        stmt = (
            self._base_stmt()
            .where(Task.team_id == team_id)
            .order_by(Task.due_date.asc(), Task.created_at.desc())
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def list_by_team_ids(self, team_ids: list[int]) -> list[Task]:
        if not team_ids:
            return []
        stmt = (
            self._base_stmt()
            .where(Task.team_id.in_(team_ids))
            .order_by(Task.due_date.asc(), Task.created_at.desc())
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def list_by_project_ids(self, project_ids: list[int]) -> list[Task]:
        if not project_ids:
            return []
        stmt = (
            self._base_stmt()
            .where(Task.project_id.in_(project_ids))
            .order_by(Task.due_date.asc(), Task.created_at.desc())
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def list_scope_by_ids(self, task_ids: list[int]) -> list[tuple[int, int | None, int | None, int | None]]:
        """Just enough of each requested task — (id, assignee_id,
        project_id, team_id) — to answer "can this caller see it", for the
        bulk Working Time endpoint (see app/api/routes/tasks.py's
        `get_task_time_summaries`). Deliberately not `_base_stmt()`: no
        eager-loaded assignee/project/team relationships are needed for an
        authorization check, so this stays a single cheap columns-only
        query regardless of how many task_ids are requested — org-scoped,
        never trusting organization_id from the request. `team_id` was
        added by the Team Manager Task-scope follow-up so that endpoint
        can apply the same "manages this task's team" rule
        can_access_task() uses, in bulk."""
        if not task_ids:
            return []
        stmt = select(Task.id, Task.assignee_id, Task.project_id, Task.team_id).where(
            Task.organization_id == self.org_id,
            Task.id.in_(task_ids),
        )
        result = await self.db.execute(stmt)
        return [tuple(row) for row in result.all()]

    async def get_by_id(self, task_id: int) -> Task | None:
        stmt = self._base_stmt().where(Task.id == task_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def create(self, payload: TaskCreate, created_by_id: int) -> Task:
        # Defense-in-depth (cross-tenant automation-assignee security fix,
        # PHASE 16): every caller of this repository — route-level
        # create_task() (which already validates upstream) AND internal
        # services that build a TaskCreate directly, like
        # app.services.automation_tasks — is protected here, so a resolver
        # defect anywhere upstream can never persist an assignee that is
        # cross-tenant, a Client, inactive, or not a member of this task's
        # Team. Raises the same AppException the route layer already
        # surfaces; a re-validation here for a route call that already
        # passed is a cheap no-op, never a behavior change for that path.
        await validate_task_assignee(
            self.db, organization_id=self.org_id,
            assignee_id=payload.assignee_id, team_id=payload.team_id,
        )
        task = Task(
            name=payload.name.strip(),
            description=payload.description,
            icon=payload.icon,
            start_date=payload.start_date,
            due_date=payload.due_date,
            status=payload.status,
            priority=getattr(payload, "priority", "medium"),
            assignee_id=payload.assignee_id,
            project_id=payload.project_id,
            team_id=payload.team_id,
            created_by_id=created_by_id,
            organization_id=self.org_id,
        )
        self.db.add(task)
        await self.db.commit()
        return await self.get_by_id(task.id)

    async def update(self, task: Task, payload: TaskUpdate) -> Task:
        data = payload.model_dump(exclude_unset=True)
        if "name" in data and data["name"]:
            data["name"] = data["name"].strip()
        fk_changed = any(k in data for k in ("assignee_id", "project_id", "team_id"))
        for key, value in data.items():
            setattr(task, key, value)
        await self.db.commit()
        await self.db.refresh(task)
        if fk_changed:
            return await self.get_by_id(task.id)
        return task

    async def delete(self, task: Task) -> None:
        await self.db.delete(task)
        await self.db.commit()
