import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.core.database import Base


class Meeting(Base):
    __tablename__ = "meetings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Distinct from `description` — the plan's "Meeting Setup" panel keeps a
    # short objective ("what does done look like for this meeting") separate
    # from a longer free-form description.
    objective: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=60, server_default="60")
    meeting_type: Mapped[str] = mapped_column(String(50), nullable=False, default="custom", server_default="custom")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="scheduled", server_default="scheduled", index=True)
    priority: Mapped[str] = mapped_column(String(20), nullable=False, default="medium", server_default="medium")
    visibility: Mapped[str] = mapped_column(String(20), nullable=False, default="team", server_default="team")
    # Base recurrence rule (which cadence this meeting repeats on). No
    # auto-generation of future instances yet — that's smart/AI-assisted
    # scheduling, explicitly Phase 2 in the plan; this just records the
    # chosen cadence so it displays and round-trips correctly.
    recurrence: Mapped[str] = mapped_column(String(20), nullable=False, default="none", server_default="none")
    # Physical location or a video-call link (Zoom/Meet/Teams/etc) — free text.
    location: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    organizer_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    # Optional — meetings are not team-specific. When set, the meeting
    # belongs to that team (shows on its Meetings tab, scopes suggested
    # tasks/issues to it); when null, it's an org-wide meeting created from
    # the standalone Meetings page, visible per `visibility` below instead.
    team_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("teams.id", ondelete="CASCADE"), nullable=True, index=True)
    # Optional — a meeting can be scoped to one project for linked-task
    # suggestions, without requiring one.
    project_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True)
    organization_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    paused_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Personal check-in speaking-order roulette (L10 meetings only) — the
    # MeetingParticipant currently picked to speak. Cleared automatically
    # (ON DELETE SET NULL) if that participant row is ever removed.
    checkin_current_participant_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("meeting_participants.id", ondelete="SET NULL", use_alter=True), nullable=True
    )
    # Pointer to whichever agenda item is "current" — advanced by the
    # "Next agenda item" host control, mirroring checkin_current_participant_id.
    current_agenda_item_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("meeting_agenda_items.id", ondelete="SET NULL", use_alter=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    organizer = relationship("User", foreign_keys=[organizer_id], lazy="selectin")
    project = relationship("Project", foreign_keys=[project_id], lazy="selectin")
    participants: Mapped[list["MeetingParticipant"]] = relationship(
        "MeetingParticipant", back_populates="meeting", lazy="selectin",
        cascade="all, delete-orphan", foreign_keys="MeetingParticipant.meeting_id",
    )
    checkin_current_participant = relationship(
        "MeetingParticipant", foreign_keys=[checkin_current_participant_id],
        lazy="selectin", viewonly=True,
    )
    agenda_items: Mapped[list["MeetingAgendaItem"]] = relationship(
        "MeetingAgendaItem", back_populates="meeting", lazy="selectin",
        cascade="all, delete-orphan", order_by="MeetingAgendaItem.sort_order",
        foreign_keys="MeetingAgendaItem.meeting_id",
    )
    notes: Mapped[list["MeetingNote"]] = relationship(
        "MeetingNote", back_populates="meeting", lazy="selectin",
        cascade="all, delete-orphan", order_by="MeetingNote.created_at",
    )
    decisions: Mapped[list["MeetingDecision"]] = relationship(
        "MeetingDecision", back_populates="meeting", lazy="selectin",
        cascade="all, delete-orphan", order_by="MeetingDecision.created_at",
    )
    meeting_tasks: Mapped[list["MeetingTask"]] = relationship(
        "MeetingTask", back_populates="meeting", lazy="selectin",
        cascade="all, delete-orphan",
    )


class MeetingParticipant(Base):
    __tablename__ = "meeting_participants"
    __table_args__ = (UniqueConstraint("meeting_id", "user_id", name="uq_meeting_participant"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    meeting_id: Mapped[int] = mapped_column(Integer, ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    # Host-controlled: is this invited person actually attending today?
    # Independent of `joined_at`, which the participant sets for themselves.
    selected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    joined_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    spoken_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # True if this turn was skipped rather than actually spoken (still counts
    # as "done" for the round, but rendered/labeled differently).
    skipped: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # Wrap-up: each attendee rates the meeting itself (self-submit only,
    # mirroring the same "click only your own avatar" rule used for joining).
    score: Mapped[Optional[float]] = mapped_column(nullable=True)
    score_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    scored_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    meeting: Mapped["Meeting"] = relationship(
        "Meeting", back_populates="participants", foreign_keys=[meeting_id],
    )
    user = relationship("User", foreign_keys=[user_id], lazy="selectin")


class MeetingAgendaItem(Base):
    __tablename__ = "meeting_agenda_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    meeting_id: Mapped[int] = mapped_column(Integer, ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    duration_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", server_default="pending")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    presenter_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    meeting: Mapped["Meeting"] = relationship(
        "Meeting", back_populates="agenda_items", foreign_keys=[meeting_id],
    )
    presenter = relationship("User", foreign_keys=[presenter_id], lazy="selectin")


class MeetingNote(Base):
    __tablename__ = "meeting_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    meeting_id: Mapped[int] = mapped_column(Integer, ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_by_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    meeting: Mapped["Meeting"] = relationship("Meeting", back_populates="notes")
    created_by = relationship("User", foreign_keys=[created_by_id], lazy="selectin")


class MeetingDecision(Base):
    __tablename__ = "meeting_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    meeting_id: Mapped[int] = mapped_column(Integer, ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    author_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    meeting: Mapped["Meeting"] = relationship("Meeting", back_populates="decisions")
    author = relationship("User", foreign_keys=[author_id], lazy="selectin")


class MeetingTask(Base):
    __tablename__ = "meeting_tasks"
    __table_args__ = (UniqueConstraint("meeting_id", "task_id", name="uq_meeting_task"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    meeting_id: Mapped[int] = mapped_column(Integer, ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False, index=True)
    task_id: Mapped[int] = mapped_column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    agenda_item_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("meeting_agenda_items.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    meeting: Mapped["Meeting"] = relationship("Meeting", back_populates="meeting_tasks")
    task = relationship("Task", foreign_keys=[task_id], lazy="selectin")
    agenda_item = relationship("MeetingAgendaItem", foreign_keys=[agenda_item_id], lazy="selectin")


class MeetingTemplate(Base):
    """A saved, reusable agenda structure ("Save Meeting Template" in the
    plan) — org-scoped. `agenda_sections` is a JSON list of
    {title, duration_minutes} objects, applied as the starting agenda when
    a user picks this template in the Create Meeting agenda builder.
    `is_builtin` marks the 9 prebuilt meeting-type templates (seeded once
    per org, same bootstrap-on-first-read pattern used by onboarding
    templates) so they're visually distinguishable from user-saved ones but
    are otherwise ordinary, editable/deletable rows."""
    __tablename__ = "meeting_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    meeting_type: Mapped[str] = mapped_column(String(50), nullable=False, default="custom", server_default="custom")
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    default_duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=60, server_default="60")
    agenda_sections: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    is_builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    created_by_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    created_by = relationship("User", foreign_keys=[created_by_id], lazy="selectin")
