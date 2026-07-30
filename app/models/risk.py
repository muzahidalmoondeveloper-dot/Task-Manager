import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.core.database import Base


class Risk(Base):
    __tablename__ = "risks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    team_id: Mapped[int] = mapped_column(Integer, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    organization_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True)
    owner_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, default="medium", server_default="medium")
    likelihood: Mapped[str] = mapped_column(String(20), nullable=False, default="medium", server_default="medium")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open", server_default="open")
    mitigation_plan: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    identified_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    project_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True)

    owner = relationship("User", foreign_keys=[owner_id], lazy="selectin")
    project = relationship("Project", foreign_keys=[project_id], lazy="selectin")
