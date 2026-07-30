import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.task_suggestion import TaskSuggestion
from app.repositories.base_tenant_repository import TenantRepository
from app.schemas.task_suggestion import ExtractedTask


class TaskSuggestionRepository(TenantRepository):
    def __init__(self, db: AsyncSession, org_id: uuid.UUID) -> None:
        super().__init__(db, org_id)

    async def list_pending(self, user_id: int) -> list[TaskSuggestion]:
        stmt = (
            select(TaskSuggestion)
            .where(
                TaskSuggestion.organization_id == self.org_id,
                TaskSuggestion.created_by_id == user_id,
                TaskSuggestion.status == "pending",
            )
            .order_by(TaskSuggestion.created_at.desc())
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def source_already_analyzed(
        self,
        *,
        source_type: str,
        source_id: int,
        created_by_id: int,
    ) -> bool:
        stmt = (
            select(func.count(TaskSuggestion.id))
            .where(
                TaskSuggestion.organization_id == self.org_id,
                TaskSuggestion.source_type == source_type,
                TaskSuggestion.source_id == source_id,
                TaskSuggestion.created_by_id == created_by_id,
            )
        )
        result = await self.db.execute(stmt)
        return (result.scalar_one() or 0) > 0

    async def get_by_id(self, suggestion_id: int) -> TaskSuggestion | None:
        stmt = select(TaskSuggestion).where(
            TaskSuggestion.id == suggestion_id,
            TaskSuggestion.organization_id == self.org_id,
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def create_many(
        self,
        *,
        source_type: str,
        source_id: int,
        tasks: list[ExtractedTask],
        raw_ai_payload: dict,
        created_by_id: int,
    ) -> list[TaskSuggestion]:
        suggestions = []
        for task in tasks:
            suggestion = TaskSuggestion(
                source_type=source_type,
                source_id=source_id,
                title=task.title,
                description=task.description,
                suggested_start_date=task.suggested_start_date,
                suggested_due_date=task.suggested_due_date,
                suggested_assignee_name=task.suggested_assignee_name,
                suggested_assignee_email=task.suggested_assignee_email,
                suggested_project_name=task.suggested_project_name,
                suggested_team_name=task.suggested_team_name,
                confidence=task.confidence,
                raw_ai_payload=raw_ai_payload,
                created_by_id=created_by_id,
                organization_id=self.org_id,
            )
            self.db.add(suggestion)
            suggestions.append(suggestion)

        await self.db.commit()
        for s in suggestions:
            await self.db.refresh(s)
        return suggestions
