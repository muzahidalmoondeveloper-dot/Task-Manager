import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Table, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


org_role_assignments = Table(
    "org_role_assignments",
    Base.metadata,
    Column("role_id", Integer, ForeignKey("org_roles.id", ondelete="CASCADE"), primary_key=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
)


class OrgRole(Base):
    __tablename__ = "org_roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    responsibilities: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    parent_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("org_roles.id", ondelete="SET NULL"), nullable=True
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Not using Mapped[] here — bare Mapped[list] makes SQLAlchemy treat this as a
    # scalar relationship, causing instance_state([]) crash in cascade_iterator.
    assignees = relationship("User", secondary=org_role_assignments, lazy="selectin")
