import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Explicit conversation state machine (spec Section 8) — idle |
    # awaiting_confirmation | awaiting_manager_approval |
    # awaiting_admin_approval. Interpreting/retrieving/etc. are transient
    # sub-states of a single request and aren't persisted between turns.
    state: Mapped[str] = mapped_column(String(30), nullable=False, default="idle", server_default="idle")
    pending_change_set_id: Mapped[int | None] = mapped_column(
        ForeignKey("ai_change_sets.id", ondelete="SET NULL"), nullable=True,
    )
    pending_approval_id: Mapped[int | None] = mapped_column(
        ForeignKey("ai_approval_requests.id", ondelete="SET NULL"), nullable=True,
    )

    # Previous result-set memory (architecture item 8) — the ordered list of
    # entity ids shown in this session's most recent list-type reply (e.g.
    # "show me overdue tasks"), so a positional follow-up ("mark the second
    # one done") can resolve deterministically against exactly what the user
    # was just shown, instead of falling through to fuzzy name matching or
    # an ambiguity prompt. Shape: {"entity_type": "task", "ids": [12, 7, 31]}.
    # Only the single most recent result set is kept (not a history) —
    # matches how positional references are actually used in conversation
    # ("the second one" always means the list just shown, never an older one).
    last_result_set_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    messages: Mapped[list["ChatMessage"]] = relationship(
        "ChatMessage",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="ChatMessage.created_at",
    )
    user = relationship("User", foreign_keys=[user_id])


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # "user" | "assistant"
    content: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    session: Mapped["ChatSession"] = relationship("ChatSession", back_populates="messages")
