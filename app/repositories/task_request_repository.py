import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.task_request import TaskRequest
from app.repositories.base_tenant_repository import TenantRepository
from app.schemas.task_request import TaskRequestCreate


class TaskRequestRepository(TenantRepository):
    def __init__(self, db: AsyncSession, org_id: uuid.UUID) -> None:
        super().__init__(db, org_id)

    def _base_stmt(self):
        return select(TaskRequest).where(TaskRequest.organization_id == self.org_id)

    async def list_for_project(self, project_id: int) -> list[TaskRequest]:
        stmt = self._base_stmt().where(TaskRequest.project_id == project_id).order_by(TaskRequest.created_at.desc())
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def get_by_id(self, request_id: int) -> TaskRequest | None:
        stmt = self._base_stmt().where(TaskRequest.id == request_id).execution_options(populate_existing=True)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def create(self, project_id: int, submitted_by_id: int, payload: TaskRequestCreate) -> TaskRequest:
        request = TaskRequest(
            organization_id=self.org_id,
            project_id=project_id,
            submitted_by_id=submitted_by_id,
            title=payload.title.strip(),
            description=payload.description,
        )
        self.db.add(request)
        await self.db.commit()
        return await self.get_by_id(request.id)

    async def mark_converted(self, request: TaskRequest, task_id: int, reviewed_by_id: int) -> TaskRequest:
        request.status = "converted"
        request.converted_task_id = task_id
        request.reviewed_by_id = reviewed_by_id
        request.reviewed_at = datetime.now(timezone.utc)
        await self.db.commit()
        return await self.get_by_id(request.id)

    async def mark_rejected(self, request: TaskRequest, reviewed_by_id: int) -> TaskRequest:
        request.status = "rejected"
        request.reviewed_by_id = reviewed_by_id
        request.reviewed_at = datetime.now(timezone.utc)
        await self.db.commit()
        return await self.get_by_id(request.id)
