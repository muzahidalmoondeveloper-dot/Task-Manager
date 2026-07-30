import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint, Uuid, func
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

    provider_message_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    sender: Mapped[str | None] = mapped_column(String(320), nullable=True)
    recipients: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)

    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    tasks_extracted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

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

    provider_event_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    organizer_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    attendees: Mapped[list[dict] | None] = mapped_column(JSONB, nullable=True)

    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    meeting_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)

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