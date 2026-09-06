import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class ActivityLog(Base):
    """Persistent, append-only audit trail of meaningful user actions
    (Task #8) — WHO did WHAT to WHICH OBJECT and WHEN. Distinct from
    Notification (a per-recipient inbox message, CASCADE-deleted with its
    task) and from Python `logging` (ephemeral process output) — this is
    structured, queryable, organization-scoped, and outlives the entities
    it describes.

    Deletion semantics (Phase 20): `actor_user_id` uses SET NULL so a
    later-deleted user's history stays readable (falls back to "Deleted
    User" — see ActivityLogRead). The affected entity is NOT a destructive
    FK at all — `entity_type` + `entity_id` are plain, non-enforced
    columns (entities logged here are heterogeneous: tasks, projects,
    teams, users, ...), and `entity_label` is a small persisted snapshot
    (e.g. a task's name at the time of the action) specifically so a
    hard-deleted task's own history still reads as "Deleted task: Prepare
    Report" instead of a bare, meaningless id.
    """

    __tablename__ = "activity_logs"
    __table_args__ = (
        Index("ix_activity_logs_org_created_at", "organization_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Immutable historical snapshot of the actor's display name AT THE
    # MOMENT this activity happened (Task #8 follow-up) — captured once, in
    # activity_service.record(), and never rewritten afterward, even if the
    # user later renames themselves or is deleted. This is what keeps
    # "WHO did WHAT" meaningful once actor_user_id goes NULL (SET NULL on
    # user deletion, never CASCADE — see class docstring); without this
    # field, a deleted actor's entire history collapsed to an anonymous
    # "Deleted User" with no way to tell WHO it used to be.
    #
    # No FK, no cascade: this is a plain, disconnected string, exactly like
    # entity_label. Nullable only because rows written before this column
    # existed can't have one (see the backfill migration for what could be
    # safely recovered after the fact vs. what's genuinely unrecoverable).
    actor_label: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Machine-readable, e.g. "task.updated" — see app/core/activity_actions.py
    # for the closed taxonomy; never an arbitrary free-form string.
    action: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

    # e.g. "task" / "project" / "team" / "user" — not a real FK (see class docstring).
    entity_type: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Small display snapshot captured at write time (e.g. a task's name) so
    # history reads meaningfully even after the entity itself is deleted —
    # never the full entity, never sensitive fields.
    entity_label: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Small, bounded, non-sensitive structured context — e.g.
    # {"field": "status", "from": "todo", "to": "done"}. Never the full
    # mutated object, never secrets (see activity_service.record()'s
    # enforcement of this).
    activity_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    actor = relationship("User", foreign_keys=[actor_user_id])
