import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.chat import ChatMessage, ChatSession
from app.repositories.base_tenant_repository import TenantRepository


class ChatRepository(TenantRepository):
    def __init__(self, db: AsyncSession, org_id: uuid.UUID) -> None:
        super().__init__(db, org_id)

    async def create_session(self, user_id: int, title: str | None = None) -> ChatSession:
        session = ChatSession(
            user_id=user_id,
            title=title,
            organization_id=self.org_id,
        )
        self.db.add(session)
        await self.db.commit()
        await self.db.refresh(session)
        return session

    async def get_session(self, session_id: int) -> ChatSession | None:
        result = await self.db.execute(
            select(ChatSession)
            .where(
                ChatSession.id == session_id,
                ChatSession.organization_id == self.org_id,
            )
            .options(selectinload(ChatSession.messages))
        )
        return result.scalar_one_or_none()

    async def list_sessions_for_user(self, user_id: int) -> list[ChatSession]:
        result = await self.db.execute(
            select(ChatSession)
            .where(
                ChatSession.user_id == user_id,
                ChatSession.organization_id == self.org_id,
            )
            .order_by(ChatSession.updated_at.desc())
        )
        return list(result.scalars().all())

    async def add_message(self, session_id: int, role: str, content: str) -> ChatMessage:
        msg = ChatMessage(session_id=session_id, role=role, content=content)
        self.db.add(msg)
        await self.db.commit()
        await self.db.refresh(msg)
        return msg

    async def get_session_messages(self, session_id: int, limit: int = 40) -> list[ChatMessage]:
        result = await self.db.execute(
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.created_at)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def update_session_title(self, session: ChatSession, title: str) -> ChatSession:
        session.title = title
        await self.db.commit()
        await self.db.refresh(session)
        return session

    async def touch_session(self, session: ChatSession) -> None:
        session.updated_at = datetime.now(timezone.utc)
        await self.db.commit()
