import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class TaskTimeEntry(Base):
    """One work session tracked against a task — the authoritative source
    of a task's (and, later, a project's) real working time. `stopped_at`
    is NULL while the session is active; `duration_seconds` is only ever
    computed and persisted once the session is stopped, so it's always
    NULL for an active entry (never a stale/incorrect number).

    Every timestamp is server-generated UTC (`datetime.now(timezone.utc)`,
    matching this app's existing convention elsewhere in tasks.py) — the
    client never supplies started_at/stopped_at, so a session's duration
    can't be fabricated or manipulated from the browser.
    """

    __tablename__ = "task_time_entries"
    __table_args__ = (
        # Declared here (not only in the Alembic migration) so this app's
        # existing dev/test provisioning path — `Base.metadata.create_all`
        # in main.py's lifespan, which this whole test suite relies on
        # instead of `alembic upgrade` — actually creates it too. Without
        # this, the concurrency guarantee below would silently not exist
        # outside a real `alembic upgrade` run.
        Index(
            "ix_task_time_entries_one_active_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("stopped_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    # CASCADE: tasks are hard-deleted in this app (TaskRepository.delete —
    # no soft-delete/archive concept exists on Task), so a time entry
    # whose task no longer exists has nothing left to be historical
    # *about*; keeping it around would only be an orphaned row an admin
    # could never make sense of. This mirrors how Organization already
    # cascades its own dependent rows in this schema.
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # RESTRICT: matches Task.created_by_id's existing convention — a time
    # entry is a historical attribution record (who did this work), the
    # same category of fact task creation already protects from silently
    # losing its "who" when a user is removed.
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # CASCADE: matches Task.organization_id's existing convention —
    # deleting an organization already cascades everything under it.
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Canonical unit is seconds (app/core convention has no stronger
    # existing precedent — Meeting.duration_minutes is a *scheduled*
    # length, a different concept entirely). Persisted only once, when the
    # session is stopped, from `stopped_at - started_at` — never derived
    # from Task.created_at/completed_at.
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    task = relationship("Task")
    user = relationship("User")

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
