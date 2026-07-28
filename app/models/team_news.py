import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class TeamNews(Base):
    __tablename__ = "team_news"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    icon: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    team_id: Mapped[int] = mapped_column(Integer, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True)
    owner_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    owner = relationship("User", foreign_keys=[owner_id], lazy="selectin")
    links: Mapped[list["TeamNewsLink"]] = relationship(
        "TeamNewsLink",
        back_populates="news",
        lazy="selectin",
        cascade="all, delete-orphan",
        order_by="TeamNewsLink.id",
    )


# Polymorphic link from a news item to an Objective, Rock, To-Do (Task), or KPI.
# `title` is a denormalized snapshot taken when the link is created/updated so
# rendering a news item never has to join across four unrelated tables.
class TeamNewsLink(Base):
    __tablename__ = "team_news_links"
    __table_args__ = (
        UniqueConstraint("news_id", "linked_type", "linked_id", name="uq_team_news_link"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    news_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("team_news.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 'objective' | 'rock' | 'task' | 'kpi'
    linked_type: Mapped[str] = mapped_column(String(20), nullable=False)
    linked_id: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    news: Mapped["TeamNews"] = relationship("TeamNews", back_populates="links")
