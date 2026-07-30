import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Note(Base):
    """A free-text note attached to a Rock, KPI, Issue, or News item.

    Generic across all four entity types via (entity_type, entity_id) —
    mirrors the polymorphic-target idiom already used by RockLink/IssueLink/
    KpiLink/TeamNewsLink, just on the owner side instead of the target side.
    """
    __tablename__ = "notes"
    __table_args__ = (
        Index("ix_notes_entity", "entity_type", "entity_id", "organization_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    entity_type: Mapped[str] = mapped_column(String(20), nullable=False)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    organization_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True)
    author_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    author = relationship("User", foreign_keys=[author_id], lazy="selectin")
