import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    logo_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    status: Mapped[str] = mapped_column(
        String(50),
        default="active",
        nullable=False,
        index=True,
    )

    created_by_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    created_by = relationship(
        "User",
        foreign_keys=[created_by_id],
    )

    memberships = relationship(
        "ProjectMembership",
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
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


class ProjectMembership(Base):
    __tablename__ = "project_memberships"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    project = relationship(
        "Project",
        back_populates="memberships",
        passive_deletes=True,
    )

    user = relationship(
        "User",
        passive_deletes=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "user_id",
            name="uq_project_membership_project_user",
        ),
    )


class ProjectTeam(Base):
    """Explicit Project<->Team assignment (Project Manager Team-selection
    bug-fix). Before this, "which teams belong to this project" was only
    ever *derived* — inferred after the fact from Rocks/KPIs/Tasks that
    already happened to carry both a project_id and a team_id. That's
    circular for a brand-new project with no Rocks/KPIs/Tasks yet: there
    was no way for a team to ever become the FIRST one tied to a project,
    so a plain Project Manager's Create-Task "Team" dropdown (scoped to
    exactly this derived set — see app.core.project_access.
    list_project_team_ids) stayed permanently empty even when the
    organization plainly had teams.

    This table is the real, explicit, queryable link — assigned via
    POST/DELETE /projects/{id}/teams/{team_id}. `list_project_team_ids()`
    now returns the UNION of this table's rows and the original derived
    associations, so nothing that worked before regresses; every project's
    valid team set is just no longer required to already contain a task."""

    __tablename__ = "project_teams"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    team_id: Mapped[int] = mapped_column(
        ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    assigned_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    project = relationship("Project", passive_deletes=True)
    team = relationship("Team", passive_deletes=True)
    assigned_by = relationship("User", foreign_keys=[assigned_by_id], passive_deletes=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "team_id",
            name="uq_project_team_project_team",
        ),
    )