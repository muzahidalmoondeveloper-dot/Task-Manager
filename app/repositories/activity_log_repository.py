from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.activity_log import ActivityLog
from app.repositories.base_tenant_repository import TenantRepository

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 50


class ActivityLogRepository(TenantRepository):
    def __init__(self, db: AsyncSession, org_id) -> None:
        super().__init__(db, org_id)

    async def list_page(
        self,
        *,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
        actor_user_id: int | None = None,
        action: str | None = None,
        entity_type: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> tuple[list[ActivityLog], int]:
        """Newest-first, paginated, bounded page size. Returns
        (items, total_count) — `total_count` reflects the filtered set (not
        just the current page) so the frontend can render real pagination
        rather than guessing from a partial page."""
        page = max(page, 1)
        page_size = max(1, min(page_size, MAX_PAGE_SIZE))

        filters = [ActivityLog.organization_id == self.org_id]
        if actor_user_id is not None:
            filters.append(ActivityLog.actor_user_id == actor_user_id)
        if action is not None:
            filters.append(ActivityLog.action == action)
        if entity_type is not None:
            filters.append(ActivityLog.entity_type == entity_type)
        if since is not None:
            filters.append(ActivityLog.created_at >= since)
        if until is not None:
            filters.append(ActivityLog.created_at <= until)

        count_stmt = select(func.count()).select_from(ActivityLog).where(*filters)
        total = int((await self.db.execute(count_stmt)).scalar_one())

        stmt = (
            select(ActivityLog)
            .where(*filters)
            .options(selectinload(ActivityLog.actor))
            .order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all()), total
