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

    async def list_for_client(self, client_user_id: int) -> list[TaskRequest]:
        """Every task request a given client has submitted, across all of
        their projects — used by the Users page's client detail view."""
        stmt = self._base_stmt().where(TaskRequest.submitted_by_id == client_user_id).order_by(TaskRequest.created_at.desc())
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def get_by_id(self, request_id: int) -> TaskRequest | None:
        stmt = self._base_stmt().where(TaskRequest.id == request_id).execution_options(populate_existing=True)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id_for_update(self, request_id: int) -> TaskRequest | None:
        """Like get_by_id(), but with `SELECT ... FOR UPDATE` — same TOCTOU
        pattern as app.services.copilot.change_sets.get_change_set_for_update
        (see its docstring). Used by BOTH convert_task_request and
        reject_task_request (Reject/Convert race-condition follow-up — they
        used to lock inconsistently, letting a Convert and a Reject fired
        at the same moment both observe status="pending" and both
        succeed): a double click, two open tabs, or two simultaneous
        Convert/Reject API calls on the same request must always leave
        exactly one final status, never both a created Task AND a
        "rejected" request. Each caller holds the lock through its own
        single commit (Task insert + status update for convert; status
        update alone for reject) so a second concurrent transaction blocks
        until the first commits, then re-reads the now-committed status
        and is correctly rejected instead of racing past the pending
        check."""
        stmt = (
            self._base_stmt()
            .where(TaskRequest.id == request_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
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

    def mark_converted_no_commit(self, request: TaskRequest, task_id: int, reviewed_by_id: int) -> None:
        """Field-setting half of mark_converted(), without the commit — so
        the caller can commit this together with the new Task's insert in
        one transaction (see get_by_id_for_update's docstring)."""
        request.status = "converted"
        request.converted_task_id = task_id
        request.reviewed_by_id = reviewed_by_id
        request.reviewed_at = datetime.now(timezone.utc)

    async def mark_converted(self, request: TaskRequest, task_id: int, reviewed_by_id: int) -> TaskRequest:
        self.mark_converted_no_commit(request, task_id, reviewed_by_id)
        await self.db.commit()
        return await self.get_by_id(request.id)

    def mark_rejected_no_commit(self, request: TaskRequest, reviewed_by_id: int) -> None:
        """Field-setting half of mark_rejected(), without the commit — so
        reject_task_request can hold its SELECT ... FOR UPDATE lock
        (get_by_id_for_update) right through to this single commit, same
        as convert_task_request does with mark_converted_no_commit."""
        request.status = "rejected"
        request.reviewed_by_id = reviewed_by_id
        request.reviewed_at = datetime.now(timezone.utc)

    async def mark_rejected(self, request: TaskRequest, reviewed_by_id: int) -> TaskRequest:
        self.mark_rejected_no_commit(request, reviewed_by_id)
        await self.db.commit()
        return await self.get_by_id(request.id)
