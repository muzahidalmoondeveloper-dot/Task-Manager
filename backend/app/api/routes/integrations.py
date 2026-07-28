from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.tenant import TenantContext, enforce_feature, get_tenant_context, require_org_admin
from app.repositories.integration_repository import IntegrationRepository
from app.schemas.integration import IntegrationAccountRead
from datetime import datetime, timedelta, timezone
from sqlalchemy import delete, select
from app.models.integration import (
    CalendarEvent,
    ImportedEmail,
    IntegrationAccount,
    MeetingTranscript,
)
from app.services.integrations.microsoft_graph import MicrosoftGraphService
from app.services.integrations.oauth import (
    calculate_expires_at,
    exchange_google_code,
    exchange_microsoft_code,
    get_google_profile,
    get_microsoft_profile,
    google_auth_url,
    microsoft_auth_url,
    read_state,
)

router = APIRouter(prefix="/integrations", tags=["Integrations"])


def parse_graph_datetime(value: dict | None):
    if not value:
        return None

    raw = value.get("dateTime")

    if not raw:
        return None

    if raw.endswith("Z"):
        raw = raw.replace("Z", "+00:00")

    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def parse_iso_datetime(value: str | None):
    if not value:
        return None

    if value.endswith("Z"):
        value = value.replace("Z", "+00:00")

    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def get_recent_range_utc(days: int = 7):
    end_at = datetime.now(timezone.utc)
    start_at = end_at - timedelta(days=days)
    return start_at, end_at


def extract_email_address(value: dict | None) -> str | None:
    if not value:
        return None

    email = value.get("emailAddress") or {}
    return email.get("address")


def extract_recipients(values: list[dict] | None) -> list[str]:
    recipients = []

    for item in values or []:
        email = extract_email_address(item)
        if email:
            recipients.append(email)

    return recipients

def get_yesterday_range_utc():
    now = datetime.now(timezone.utc)

    today_start = datetime(
        year=now.year,
        month=now.month,
        day=now.day,
        tzinfo=timezone.utc,
    )

    yesterday_start = today_start - timedelta(days=1)

    return yesterday_start, now


@router.get("/accounts", response_model=list[IntegrationAccountRead])
async def list_connected_accounts(
    tenant: TenantContext = Depends(get_tenant_context),
):
    enforce_feature(tenant, "has_integrations")
    repository = IntegrationRepository(tenant.db, tenant.organization_id)
    accounts = await repository.list_accounts(tenant.user.id)
    return [IntegrationAccountRead.model_validate(account) for account in accounts]


@router.get("/google/connect")
async def connect_google(tenant: TenantContext = Depends(get_tenant_context)):
    enforce_feature(tenant, "has_integrations")
    return {"url": google_auth_url(tenant.user.id, org_id=str(tenant.organization_id))}


@router.get("/microsoft/connect")
async def connect_microsoft(tenant: TenantContext = Depends(get_tenant_context)):
    enforce_feature(tenant, "has_integrations")
    return {"url": microsoft_auth_url(tenant.user.id, org_id=str(tenant.organization_id))}


@router.get("/google/callback")
async def google_callback(
    code: str = Query(...),
    state: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    # OAuth callbacks cannot carry JWT auth — they use the user_id + org_id baked into state.
    state_data = read_state(state)

    if state_data.get("provider") != "google":
        raise HTTPException(status_code=400, detail="Invalid OAuth state.")

    token_data = await exchange_google_code(code)
    profile = await get_google_profile(token_data["access_token"])

    email = profile.get("email")

    if not email:
        raise HTTPException(status_code=400, detail="Google account email not found.")

    import uuid as _uuid
    org_id_raw = state_data.get("org_id")
    org_id = _uuid.UUID(str(org_id_raw)) if org_id_raw else None

    from sqlalchemy import select as _select
    from app.models.organization import Organization
    org = (await db.execute(_select(Organization).where(Organization.id == org_id))).scalar_one_or_none() if org_id else None
    if org is None:
        raise HTTPException(status_code=400, detail="Invalid organization context in OAuth state.")

    repository = IntegrationRepository(db, org.id)

    await repository.upsert_account(
        user_id=state_data["user_id"],
        provider="google",
        account_email=email,
        access_token=token_data["access_token"],
        refresh_token=token_data.get("refresh_token"),
        expires_at=calculate_expires_at(token_data.get("expires_in")),
        scopes=token_data.get("scope", "").split(" "),
    )

    return RedirectResponse(f"{settings.FRONTEND_URL}/integrations?connected=google")



@router.delete("/accounts/{account_id}")
async def disconnect_integration_account(
    account_id: int,
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db),
):
    enforce_feature(tenant, "has_integrations")
    result = await db.execute(
        select(IntegrationAccount).where(
            IntegrationAccount.id == account_id,
            IntegrationAccount.user_id == tenant.user.id,
            IntegrationAccount.organization_id == tenant.organization_id,
        )
    )

    account = result.scalar_one_or_none()

    if not account:
        raise HTTPException(
            status_code=404,
            detail="Integration account not found.",
        )

    calendar_event_ids = select(CalendarEvent.id).where(
        CalendarEvent.integration_account_id == account.id
    )

    await db.execute(
        delete(MeetingTranscript).where(
            MeetingTranscript.calendar_event_id.in_(calendar_event_ids)
        )
    )

    await db.execute(
        delete(CalendarEvent).where(
            CalendarEvent.integration_account_id == account.id
        )
    )

    await db.execute(
        delete(ImportedEmail).where(
            ImportedEmail.integration_account_id == account.id
        )
    )

    await db.execute(
        delete(IntegrationAccount).where(
            IntegrationAccount.id == account.id
        )
    )

    await db.commit()

    return {
        "message": f"{account.provider.capitalize()} account disconnected.",
        "provider": account.provider,
        "account_email": account.account_email,
    }


@router.get("/microsoft/callback")
async def microsoft_callback(
    code: str = Query(...),
    state: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    state_data = read_state(state)

    if state_data.get("provider") != "microsoft":
        raise HTTPException(status_code=400, detail="Invalid OAuth state.")

    token_data = await exchange_microsoft_code(code)
    profile = await get_microsoft_profile(token_data["access_token"])

    email = profile.get("mail") or profile.get("userPrincipalName")

    if not email:
        raise HTTPException(status_code=400, detail="Microsoft account email not found.")

    import uuid as _uuid2
    org_id_raw2 = state_data.get("org_id")
    org_id2 = _uuid2.UUID(str(org_id_raw2)) if org_id_raw2 else None
    from sqlalchemy import select as _select2
    from app.models.organization import Organization as _Org2
    org2 = (await db.execute(_select2(_Org2).where(_Org2.id == org_id2))).scalar_one_or_none() if org_id2 else None
    if org2 is None:
        raise HTTPException(status_code=400, detail="Invalid organization context in OAuth state.")
    repository = IntegrationRepository(db, org2.id)

    await repository.upsert_account(
        user_id=state_data["user_id"],
        provider="microsoft",
        account_email=email,
        access_token=token_data["access_token"],
        refresh_token=token_data.get("refresh_token"),
        expires_at=calculate_expires_at(token_data.get("expires_in")),
        scopes=token_data.get("scope", "").split(" "),
    )

    return RedirectResponse(f"{settings.FRONTEND_URL}/integrations?connected=microsoft")


@router.post("/microsoft/sync-recent")
async def sync_recent_microsoft_data(
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db),
):
    enforce_feature(tenant, "has_integrations")
    repository = IntegrationRepository(db, tenant.organization_id)

    accounts = await repository.list_accounts_by_provider(
        tenant.user.id,
        "microsoft",
    )

    if not accounts:
        raise HTTPException(
            status_code=400,
            detail="No Microsoft account connected for this user.",
        )

    start_at, end_at = get_yesterday_range_utc()

    total_emails = 0
    total_events = 0
    total_transcripts = 0

    transcript_errors = []
    transcript_debug = []

    print("========== MICROSOFT SYNC START ==========")
    print("Current user:", current_user.id, current_user.email)
    print("Date range:", start_at.isoformat(), "to", end_at.isoformat())
    print("Microsoft accounts:", [account.account_email for account in accounts])

    for account in accounts:
        graph = MicrosoftGraphService(account)
        await graph.ensure_fresh_token(db)

        print("----- SYNCING ACCOUNT -----")
        print("Account:", account.account_email)

        # -------------------------
        # 1. Import Outlook emails
        # -------------------------
        try:
            messages = await graph.list_messages(start_at, end_at)
            print("Messages found:", len(messages))
        except Exception as exc:
            print("Email sync error:", str(exc))
            messages = []

        for message in messages:
            body = message.get("body") or {}
            sender = extract_email_address(message.get("from"))
            recipients = extract_recipients(message.get("toRecipients"))

            await repository.upsert_imported_email(
                integration_account_id=account.id,
                provider_message_id=message["id"],
                subject=message.get("subject"),
                sender=sender,
                recipients=recipients,
                received_at=parse_iso_datetime(message.get("receivedDateTime")),
                snippet=message.get("bodyPreview"),
                body_text=body.get("content") or message.get("bodyPreview"),
                raw_payload=message,
            )

            total_emails += 1

        # -------------------------
        # 2. Import calendar events
        # -------------------------
        try:
            events = await graph.list_calendar_events(start_at, end_at)
            print("Calendar events found:", len(events))
        except Exception as exc:
            print("Calendar sync error:", str(exc))
            events = []

        for event in events:
            organizer = event.get("organizer") or {}
            organizer_email = extract_email_address(organizer)

            attendees = []

            for attendee in event.get("attendees") or []:
                email = extract_email_address(attendee)

                attendees.append(
                    {
                        "email": email,
                        "name": (attendee.get("emailAddress") or {}).get("name"),
                        "type": attendee.get("type"),
                    }
                )

            online_meeting = event.get("onlineMeeting") or {}

            meeting_url = (
                event.get("onlineMeetingUrl")
                or online_meeting.get("joinUrl")
                or event.get("webLink")
            )

            saved_event = await repository.upsert_calendar_event(
                integration_account_id=account.id,
                provider_event_id=event["id"],
                title=event.get("subject"),
                organizer_email=organizer_email,
                attendees=attendees,
                starts_at=parse_graph_datetime(event.get("start")),
                ends_at=parse_graph_datetime(event.get("end")),
                meeting_url=meeting_url,
                provider="microsoft",
                raw_payload=event,
            )

            total_events += 1

            # -------------------------
            # 3. Resolve Teams meeting ID
            # -------------------------
            event_title = event.get("subject")
            online_meeting_id = online_meeting.get("id")

            debug_item = {
                "event_id": event.get("id"),
                "title": event_title,
                "meeting_url": meeting_url,
                "online_meeting_from_event": online_meeting,
                "online_meeting_id_from_event": online_meeting_id,
                "resolved_online_meeting_id": None,
                "transcript_count": 0,
                "status": "pending",
            }

            print("========== EVENT DEBUG ==========")
            print("Event title:", event_title)
            print("Meeting URL:", meeting_url)
            print("onlineMeeting from event:", online_meeting)
            print("onlineMeeting ID from event:", online_meeting_id)

            if not online_meeting_id and meeting_url:
                try:
                    print("Trying to resolve online meeting by join URL...")

                    found_meeting = await graph.find_online_meeting_by_join_url(
                        meeting_url
                    )

                    print("Found meeting by join URL:", found_meeting)

                    if found_meeting:
                        online_meeting_id = found_meeting.get("id")

                except Exception as exc:
                    error_message = str(exc)

                    print("find_online_meeting_by_join_url error:", error_message)

                    transcript_errors.append(
                        {
                            "event_id": event.get("id"),
                            "title": event_title,
                            "meeting_url": meeting_url,
                            "stage": "resolve_online_meeting_by_join_url",
                            "error": error_message,
                        }
                    )

            debug_item["resolved_online_meeting_id"] = online_meeting_id

            if not online_meeting_id:
                debug_item["status"] = "skipped_no_online_meeting_id"

                print("Skipping transcript fetch. No onlineMeeting ID found.")

                transcript_debug.append(debug_item)
                continue

            # -------------------------
            # 4. Fetch transcript metadata
            # -------------------------
            try:
                transcripts = await graph.list_transcripts_for_online_meeting(
                    online_meeting_id
                )

                debug_item["transcript_count"] = len(transcripts)

                print("Transcript count:", len(transcripts))

            except Exception as exc:
                error_message = str(exc)

                debug_item["status"] = "failed_list_transcripts"
                debug_item["error"] = error_message

                print("Transcript list error:", error_message)

                transcript_errors.append(
                    {
                        "event_id": event.get("id"),
                        "title": event_title,
                        "online_meeting_id": online_meeting_id,
                        "meeting_url": meeting_url,
                        "stage": "list_transcripts",
                        "error": error_message,
                    }
                )

                transcript_debug.append(debug_item)
                continue

            if not transcripts:
                debug_item["status"] = "no_transcripts_returned"

                print("No transcripts returned for this meeting.")

                transcript_debug.append(debug_item)
                continue

            # -------------------------
            # 5. Fetch transcript content
            # -------------------------
            for transcript in transcripts:
                transcript_id = transcript.get("id")

                print("Transcript metadata:", transcript)

                if not transcript_id:
                    transcript_errors.append(
                        {
                            "event_id": event.get("id"),
                            "title": event_title,
                            "online_meeting_id": online_meeting_id,
                            "meeting_url": meeting_url,
                            "stage": "missing_transcript_id",
                            "transcript": transcript,
                        }
                    )
                    continue

                try:
                    transcript_text = await graph.get_transcript_content(
                        online_meeting_id,
                        transcript_id,
                    )

                    print("Transcript text preview:", transcript_text[:1000])

                    await repository.upsert_meeting_transcript(
                        calendar_event_id=saved_event.id,
                        provider_transcript_id=transcript_id,
                        transcript_text=transcript_text,
                        raw_payload=transcript,
                    )

                    total_transcripts += 1
                    debug_item["status"] = "transcript_imported"

                except Exception as exc:
                    error_message = str(exc)

                    print("Transcript content error:", error_message)

                    transcript_errors.append(
                        {
                            "event_id": event.get("id"),
                            "title": event_title,
                            "online_meeting_id": online_meeting_id,
                            "meeting_url": meeting_url,
                            "transcript_id": transcript_id,
                            "stage": "get_transcript_content",
                            "error": error_message,
                        }
                    )

                    debug_item["status"] = "failed_get_transcript_content"
                    debug_item["error"] = error_message

            transcript_debug.append(debug_item)

    print("========== MICROSOFT SYNC END ==========")
    print("Emails imported:", total_emails)
    print("Calendar events imported:", total_events)
    print("Transcripts imported:", total_transcripts)
    print("Transcript errors:", transcript_errors)

    return {
        "period": {
            "start_at": start_at.isoformat(),
            "end_at": end_at.isoformat(),
        },
        "emails_imported": total_emails,
        "calendar_events_imported": total_events,
        "transcripts_imported": total_transcripts,
        "transcript_errors": transcript_errors,
        "transcript_debug": transcript_debug,
        "message": (
            f"Imported {total_emails} emails, "
            f"{total_events} calendar events, "
            f"{total_transcripts} transcripts."
        ),
    }