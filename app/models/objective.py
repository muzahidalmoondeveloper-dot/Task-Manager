import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Objective(Base):
    __tablename__ = "objectives"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    icon: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="active", server_default="active", nullable=False)
    progress: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    owner: Mapped["User"] = relationship("User", foreign_keys=[owner_id])  # type: ignore[name-defined]
    project: Mapped["Project"] = relationship("Project", foreign_keys=[project_id])  # type: ignore[name-defined]
    created_by: Mapped["User"] = relationship("User", foreign_keys=[created_by_id])  # type: ignore[name-defined]
