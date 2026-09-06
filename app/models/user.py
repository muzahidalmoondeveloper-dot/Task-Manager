import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.org_roles import TEAM_MEMBER


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    full_name: Mapped[str] = mapped_column(String(255), nullable=False)

    email: Mapped[str] = mapped_column(
        String(320),
        unique=True,
        index=True,
        nullable=False,
    )

    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)

    role: Mapped[str] = mapped_column(
        String(50),
        default=TEAM_MEMBER,
        nullable=False,
        index=True,
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    # Servable `/media/...` URL of the user's uploaded avatar (see
    # app/services/avatar_upload_service.py) — null until the user uploads
    # one, in which case the frontend falls back to initials.
    profile_picture_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_teams = relationship(
        "Team",
        foreign_keys="Team.created_by_id",
        back_populates="created_by",
    )

    managed_teams = relationship(
        "Team",
        foreign_keys="Team.team_manager_id",
        back_populates="team_manager",
    )

    team_memberships = relationship(
        "TeamMembership",
        back_populates="user",
        cascade="all, delete-orphan",
    )

    email_verified_at: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True),
    nullable=True,
    )

    last_login_otp_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Remembers the last org the user was active in — restored on next login
    last_active_organization_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

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