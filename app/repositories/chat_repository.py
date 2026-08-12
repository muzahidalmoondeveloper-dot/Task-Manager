import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
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
        """The most recent `limit` messages, returned in chronological
        (oldest-first) order for prompt/UI consumption.

        BUG FIX (architecture Section 4.1): this used to be
        `ORDER BY created_at ASC LIMIT N`, which returns the OLDEST N
        messages in a session — for any conversation longer than `limit`,
        the model was permanently stuck seeing only the first N messages
        ever sent, never the current context. Correct behavior is to select
        the latest N (DESC + LIMIT), then reverse them back into
        chronological order. `created_at` alone isn't a safe unique sort key
        when multiple rows share a timestamp (sub-millisecond inserts), so
        `id` is used as a deterministic tiebreaker in both directions.
        """
        result = await self.db.execute(
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
            .limit(limit)
        )
        latest_newest_first = list(result.scalars().all())
        return list(reversed(latest_newest_first))

    async def update_session_title(self, session: ChatSession, title: str) -> ChatSession:
        session.title = title
        await self.db.commit()
        await self.db.refresh(session)
        return session

    async def touch_session(self, session_id: int) -> None:
        """Takes a plain int, not the ChatSession ORM object, deliberately —
        by the time this is called (end of a chat turn) `self.db`'s session
        may already have had a commit()/rollback() fire deep inside a tool
        call earlier in the same request (e.g. a multi-goal turn's first
        step), which expires every ORM object attached to it. Passing the
        object and doing `session.updated_at = ...` would still generally
        work (assignment doesn't itself trigger a reload), but reading
        `session.id` to get here in the first place is exactly the
        MissingGreenlet crash this signature avoids by construction — see
        ChatService.handle_message()'s matching comment. A direct
        UPDATE...WHERE id=... needs no ORM object at all.
        """
        await self.db.execute(
            update(ChatSession).where(ChatSession.id == session_id).values(updated_at=datetime.now(timezone.utc))
        )
        await self.db.commit()
