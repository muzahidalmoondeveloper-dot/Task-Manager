import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.note import Note
from app.repositories.base_tenant_repository import TenantRepository


class NoteRepository(TenantRepository):
    def __init__(self, db: AsyncSession, org_id: uuid.UUID) -> None:
        super().__init__(db, org_id)

    def _base_stmt(self):
        return select(Note).where(Note.organization_id == self.org_id)

    async def list_for_entity(self, entity_type: str, entity_id: int) -> list[Note]:
        stmt = (
            self._base_stmt()
            .where(Note.entity_type == entity_type, Note.entity_id == entity_id)
            .order_by(Note.created_at.desc())
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def count_for_entities(self, entity_type: str, entity_ids: list[int]) -> dict[int, int]:
        if not entity_ids:
            return {}
        stmt = (
            select(Note.entity_id, func.count(Note.id))
            .where(
                Note.organization_id == self.org_id,
                Note.entity_type == entity_type,
                Note.entity_id.in_(entity_ids),
            )
            .group_by(Note.entity_id)
        )
        result = await self.db.execute(stmt)
        return {entity_id: count for entity_id, count in result.all()}

    async def get_by_id(self, note_id: int) -> Note | None:
        stmt = self._base_stmt().where(Note.id == note_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def create(self, entity_type: str, entity_id: int, author_id: int, text: str) -> Note:
        note = Note(
            organization_id=self.org_id,
            entity_type=entity_type,
            entity_id=entity_id,
            author_id=author_id,
            text=text,
        )
        self.db.add(note)
        await self.db.commit()
        await self.db.refresh(note, attribute_names=["author"])
        return note

    async def delete(self, note: Note) -> None:
        await self.db.delete(note)
        await self.db.commit()
