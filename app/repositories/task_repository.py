import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

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
    ) -> list[Task]:
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

    async def get_by_id(self, task_id: int) -> Task | None:
        stmt = self._base_stmt().where(Task.id == task_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def create(self, payload: TaskCreate, created_by_id: int) -> Task:
        task = Task(
            name=payload.name.strip(),
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
