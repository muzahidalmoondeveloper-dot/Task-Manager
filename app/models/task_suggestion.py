import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, String, Text, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class TaskSuggestion(Base):
    __tablename__ = "task_suggestions"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    source_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    # email, transcript

    source_id: Mapped[int] = mapped_column(nullable=False, index=True)

    title: Mapped[str] = mapped_column(String(255), nullable=False)

    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    suggested_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    suggested_due_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    suggested_assignee_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    suggested_assignee_email: Mapped[str | None] = mapped_column(String(320), nullable=True)

    suggested_project_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    suggested_team_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    confidence: Mapped[str] = mapped_column(String(50), default="medium", nullable=False)
    # low, medium, high

    status: Mapped[str] = mapped_column(String(50), default="pending", nullable=False, index=True)
    # pending, approved, rejected

    raw_ai_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    approved_task_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    created_by_id: Mapped[int] = mapped_column(
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

    approved_task = relationship("Task", foreign_keys=[approved_task_id])

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