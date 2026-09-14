import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class IntegrationAccount(Base):
    __tablename__ = "integration_accounts"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    provider: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    # google, microsoft

    account_email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)

    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)

    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    scopes: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Automation Pipeline Audit follow-up — incremental-sync checkpoint and
    # at-a-glance last-run summary, so the Integrations page can answer
    # "when did it last successfully sync" and "was the last run OK"
    # without joining SyncRun every time. Only advanced on a run that
    # completed its FETCH stage without a fatal error (see
    # automation_tasks.py) — a failed sync must never skip over
    # unprocessed data.
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)

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

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "provider",
            "account_email",
            name="uq_integration_user_provider_email",
        ),
    )


class ImportedEmail(Base):
    __tablename__ = "imported_emails"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    integration_account_id: Mapped[int] = mapped_column(
        ForeignKey("integration_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    integration_account = relationship("IntegrationAccount", lazy="selectin")

    provider_message_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    sender: Mapped[str | None] = mapped_column(String(320), nullable=True)
    recipients: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)

    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    tasks_extracted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Automation Pipeline Audit follow-up — observable lifecycle beyond
    # the boolean tasks_extracted flag: discovered -> analyzing ->
    # {no_action_required | needs_review | task_created |
    # partially_created | failed}. `processing_status` transitioning OUT
    # of "discovered" into "analyzing" is the atomic CLAIM
    # (analyze_pending_sources_for_user does this with a single
    # UPDATE ... WHERE processing_status = 'discovered' ... RETURNING) —
    # the actual concurrency guard against two overlapping sync runs
    # double-processing the same email, not merely tasks_extracted itself.
    processing_status: Mapped[str] = mapped_column(String(30), nullable=False, default="discovered", server_default="discovered")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # A short (<=500 char), safe-to-display summary of the AI's own
    # `reason`/category — never the email body — surfaced as the
    # "AI Result" column in Automation Activity.
    ai_result_summary: Mapped[str | None] = mapped_column(String(500), nullable=True)

    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "integration_account_id",
            "provider_message_id",
            name="uq_imported_email_account_message",
        ),
    )


class CalendarEvent(Base):
    __tablename__ = "calendar_events"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    integration_account_id: Mapped[int] = mapped_column(
        ForeignKey("integration_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    integration_account = relationship("IntegrationAccount", lazy="selectin")

    provider_event_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    organizer_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    attendees: Mapped[list[dict] | None] = mapped_column(JSONB, nullable=True)

    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    meeting_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)

    # Automation Pipeline Audit follow-up — Teams transcript availability
    # is asynchronous: a meeting ending does NOT guarantee the transcript
    # is instantly available. `transcript_status` + `transcript_attempts`
    # + `transcript_next_check_at` implement bounded, capped-backoff
    # re-checking (see app.core.automation_pipeline's
    # TRANSCRIPT_RETRY_BACKOFF_MINUTES) instead of either an infinite
    # poll or treating "not ready yet" as an immediate permanent failure.
    transcript_status: Mapped[str] = mapped_column(String(30), nullable=False, default="unknown", server_default="unknown")
    transcript_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    transcript_next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    transcripts = relationship(
        "MeetingTranscript",
        back_populates="calendar_event",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "integration_account_id",
            "provider_event_id",
            name="uq_calendar_event_account_event",
        ),
    )


class MeetingTranscript(Base):
    __tablename__ = "meeting_transcripts"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    calendar_event_id: Mapped[int] = mapped_column(
        ForeignKey("calendar_events.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    provider_transcript_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    transcript_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    tasks_extracted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Automation Pipeline Audit follow-up — same lifecycle/claim fields as
    # ImportedEmail above (see its own docstring comment).
    processing_status: Mapped[str] = mapped_column(String(30), nullable=False, default="discovered", server_default="discovered")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_result_summary: Mapped[str | None] = mapped_column(String(500), nullable=True)

    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    calendar_event = relationship(
        "CalendarEvent",
        back_populates="transcripts",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class SyncRun(Base):
    """Automation Pipeline Audit follow-up — the persistent "Sync Run"
    concept the spec's OBSERVABILITY section asks for. ONE row per
    provider sync attempt (manual button click, or one scheduler tick for
    one user/account), so "when did it last sync / what happened" is
    answerable from the database, never only from browser state or logs
    that scroll away.

    Deliberately scoped per-account (`organization_id` +
    `integration_account_id`), matching how sync already runs (per
    user's connected account), and keeping "why does this Task exist"
    traceable to one concrete run via TaskSource.sync_run_id below."""
    __tablename__ = "automation_sync_runs"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False, index=True)  # "google" | "microsoft"
    integration_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("integration_accounts.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    triggered_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    trigger: Mapped[str] = mapped_column(String(20), nullable=False)  # manual | scheduled | retry

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued", server_default="queued", index=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    fetched_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    analyzed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    actionable_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    tasks_created_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    duplicate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    # Safe, user-facing classification only — see app.core.automation_pipeline.
    # Never a raw exception string, never a token.
    last_error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)

    correlation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), server_default=func.gen_random_uuid(), nullable=False, index=True,
    )

    integration_account = relationship("IntegrationAccount", lazy="selectin")


class TaskSource(Base):
    """Automation Pipeline Audit follow-up — Source -> Task provenance.
    Answers "why does this Task exist" without duplicating the source's
    full content into the Task record. One row per AI-created Task (a
    source with multiple action items produces multiple Tasks, each with
    its OWN TaskSource row pointing back at the same source item) —
    `task_id` is unique because a Task has exactly one origin.

    Deliberately stores only safe DISPLAY metadata (title, date) — never
    the email body or transcript text, which stay on ImportedEmail /
    MeetingTranscript, themselves already access-controlled by
    organization membership through IntegrationAccount.
    """
    __tablename__ = "task_sources"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, unique=True, index=True,
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True,
    )

    provider: Mapped[str] = mapped_column(String(50), nullable=False)  # google | microsoft
    source_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    # gmail_email | microsoft_outlook_email | microsoft_teams_transcript

    source_external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    integration_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("integration_accounts.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    sync_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("automation_sync_runs.id", ondelete="SET NULL"), nullable=True, index=True,
    )

    ai_generated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    confidence: Mapped[str | None] = mapped_column(String(10), nullable=True)  # low | medium | high

    # Safe display-only metadata (never full body/transcript text).
    source_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    task = relationship("Task", lazy="selectin")