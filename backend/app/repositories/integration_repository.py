import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.integration import (
    CalendarEvent,
    ImportedEmail,
    IntegrationAccount,
    MeetingTranscript,
)
from app.repositories.base_tenant_repository import TenantRepository


class IntegrationRepository(TenantRepository):
    def __init__(self, db: AsyncSession, org_id: uuid.UUID) -> None:
        super().__init__(db, org_id)

    async def list_accounts(self, user_id: int) -> list[IntegrationAccount]:
        stmt = (
            select(IntegrationAccount)
            .where(
                IntegrationAccount.user_id == user_id,
                IntegrationAccount.organization_id == self.org_id,
                IntegrationAccount.is_active.is_(True),
            )
            .order_by(IntegrationAccount.created_at.desc())
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def get_by_id(self, account_id: int) -> IntegrationAccount | None:
        stmt = select(IntegrationAccount).where(
            IntegrationAccount.id == account_id,
            IntegrationAccount.organization_id == self.org_id,
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def list_accounts_by_provider(self, user_id: int, provider: str) -> list[IntegrationAccount]:
        stmt = (
            select(IntegrationAccount)
            .where(
                IntegrationAccount.user_id == user_id,
                IntegrationAccount.provider == provider,
                IntegrationAccount.organization_id == self.org_id,
                IntegrationAccount.is_active.is_(True),
            )
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def upsert_account(
        self,
        *,
        user_id: int,
        provider: str,
        account_email: str,
        access_token: str,
        refresh_token: str | None,
        expires_at,
        scopes: list[str] | None,
    ) -> IntegrationAccount:
        stmt = select(IntegrationAccount).where(
            IntegrationAccount.user_id == user_id,
            IntegrationAccount.provider == provider,
            IntegrationAccount.account_email == account_email,
            IntegrationAccount.organization_id == self.org_id,
        )
        result = await self.db.execute(stmt)
        account = result.scalar_one_or_none()

        if account is None:
            account = IntegrationAccount(
                user_id=user_id,
                provider=provider,
                account_email=account_email,
                access_token=access_token,
                refresh_token=refresh_token,
                expires_at=expires_at,
                scopes=scopes,
                is_active=True,
                organization_id=self.org_id,
            )
            self.db.add(account)
        else:
            account.access_token = access_token
            account.refresh_token = refresh_token or account.refresh_token
            account.expires_at = expires_at
            account.scopes = scopes
            account.is_active = True

        await self.db.commit()
        await self.db.refresh(account)
        return account

    # ── Remaining methods are not org-scoped (they work via integration_account_id FK) ──

    async def upsert_imported_email(self, *, integration_account_id, provider_message_id, subject, sender, recipients, received_at, snippet, body_text, raw_payload) -> ImportedEmail:
        stmt = select(ImportedEmail).where(
            ImportedEmail.integration_account_id == integration_account_id,
            ImportedEmail.provider_message_id == provider_message_id,
        )
        result = await self.db.execute(stmt)
        email = result.scalar_one_or_none()
        if email is None:
            email = ImportedEmail(integration_account_id=integration_account_id, provider_message_id=provider_message_id, subject=subject, sender=sender, recipients=recipients, received_at=received_at, snippet=snippet, body_text=body_text, raw_payload=raw_payload)
            self.db.add(email)
        else:
            email.subject = subject; email.sender = sender; email.recipients = recipients
            email.received_at = received_at; email.snippet = snippet; email.body_text = body_text; email.raw_payload = raw_payload
        await self.db.commit()
        await self.db.refresh(email)
        return email

    async def upsert_calendar_event(self, *, integration_account_id, provider_event_id, title, organizer_email, attendees, starts_at, ends_at, meeting_url, provider, raw_payload) -> CalendarEvent:
        stmt = select(CalendarEvent).where(CalendarEvent.integration_account_id == integration_account_id, CalendarEvent.provider_event_id == provider_event_id)
        result = await self.db.execute(stmt)
        event = result.scalar_one_or_none()
        if event is None:
            event = CalendarEvent(integration_account_id=integration_account_id, provider_event_id=provider_event_id, title=title, organizer_email=organizer_email, attendees=attendees, starts_at=starts_at, ends_at=ends_at, meeting_url=meeting_url, provider=provider, raw_payload=raw_payload)
            self.db.add(event)
        else:
            event.title = title; event.organizer_email = organizer_email; event.attendees = attendees
            event.starts_at = starts_at; event.ends_at = ends_at; event.meeting_url = meeting_url; event.raw_payload = raw_payload
        await self.db.commit()
        await self.db.refresh(event)
        return event

    async def upsert_meeting_transcript(self, *, calendar_event_id, provider_transcript_id, transcript_text, raw_payload) -> MeetingTranscript:
        stmt = select(MeetingTranscript).where(MeetingTranscript.calendar_event_id == calendar_event_id, MeetingTranscript.provider_transcript_id == provider_transcript_id)
        result = await self.db.execute(stmt)
        transcript = result.scalar_one_or_none()
        if transcript is None:
            transcript = MeetingTranscript(calendar_event_id=calendar_event_id, provider_transcript_id=provider_transcript_id, transcript_text=transcript_text, raw_payload=raw_payload)
            self.db.add(transcript)
        else:
            transcript.transcript_text = transcript_text; transcript.raw_payload = raw_payload
        await self.db.commit()
        await self.db.refresh(transcript)
        return transcript
