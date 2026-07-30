import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.core.database import Base


class Rock(Base):
    __tablename__ = "rocks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    icon: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="backlog", server_default="backlog")
    team_id: Mapped[int] = mapped_column(Integer, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    owner_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    objective_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("objectives.id", ondelete="SET NULL"), nullable=True)
    organization_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True)
    due_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    tags: Mapped[Optional[list]] = mapped_column(JSON, nullable=True, default=list)
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    project_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True)

    owner = relationship("User", foreign_keys=[owner_id], lazy="selectin")
    objective = relationship("Objective", foreign_keys=[objective_id], lazy="selectin")
    project = relationship("Project", foreign_keys=[project_id], lazy="selectin")
    milestones = relationship(
        "Milestone",
        back_populates="rock",
        lazy="selectin",
        order_by="Milestone.sort_order",
        cascade="all, delete-orphan",
    )
    links: Mapped[list["RockLink"]] = relationship(
        "RockLink",
        back_populates="rock",
        lazy="selectin",
        cascade="all, delete-orphan",
        order_by="RockLink.id",
    )


class Milestone(Base):
    __tablename__ = "milestones"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    rock_id: Mapped[int] = mapped_column(Integer, ForeignKey("rocks.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", server_default="pending")
    due_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    owner_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    # Report-module timeline fields (planned vs. actual vs. forecast dates)
    planned_start_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    planned_end_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    actual_start_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    actual_end_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    forecast_end_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    rock = relationship("Rock", back_populates="milestones")
    owner = relationship("User", foreign_keys=[owner_id], lazy="selectin")


class RockLink(Base):
    __tablename__ = "rock_links"
    __table_args__ = (
        UniqueConstraint("rock_id", "linked_type", "linked_id", name="uq_rock_link"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    rock_id: Mapped[int] = mapped_column(Integer, ForeignKey("rocks.id", ondelete="CASCADE"), nullable=False, index=True)
    linked_type: Mapped[str] = mapped_column(String(20), nullable=False)
    linked_id: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    rock: Mapped["Rock"] = relationship("Rock", back_populates="links")
