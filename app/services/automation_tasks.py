import logging
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select, update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.auth_errors import AppException
from app.core.automation_pipeline import (
    ERROR_AI_ANALYSIS_FAILED,
    ERROR_PROVIDER_UNAVAILABLE,
    ERROR_TRANSCRIPT_PERMISSION_DENIED,
    SOURCE_STATUS_ANALYZING,
    SOURCE_STATUS_DISCOVERED,
    SOURCE_STATUS_FAILED,
    SOURCE_STATUS_NEEDS_REVIEW,
    SOURCE_STATUS_NO_ACTION_REQUIRED,
    SOURCE_STATUS_PARTIALLY_CREATED,
    SOURCE_STATUS_TASK_CREATED,
    SYNC_RUN_FAILED,
    SYNC_RUN_PARTIAL_SUCCESS,
    SYNC_RUN_RUNNING,
    SYNC_RUN_SUCCESS,
    TRANSCRIPT_STATUS_AVAILABLE,
    TRANSCRIPT_STATUS_NOT_SUPPORTED,
    TRANSCRIPT_STATUS_PERMISSION_DENIED,
    TRANSCRIPT_STATUS_UNKNOWN,
    TRANSCRIPT_STATUS_WAITING,
    classify_http_error,
    meets_auto_create_bar,
    safe_error_message,
    transcript_backoff_minutes,
)
from app.models.organization import Organization, OrganizationMembership
from app.models.integration import (
    CalendarEvent,
    ImportedEmail,
    IntegrationAccount,
    MeetingTranscript,
    SyncRun,
    TaskSource,
)
from app.models.user import User
from app.models.project import Project
from app.models.team import Team
from app.repositories.integration_repository import IntegrationRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.task_repository import TaskRepository
from app.repositories.team_repository import TeamRepository
from app.repositories.user_repository import UserRepository
from app.core.org_roles import ADMIN
from app.schemas.task import TaskCreate
from app.services.ai_task_extractor import AITaskExtractor
from app.services.integrations.microsoft_graph import MicrosoftGraphService

logger = logging.getLogger("automation_tasks")

# First-ever sync for a newly connected account has no checkpoint yet —
# bootstrap with a bounded lookback window rather than importing the
# account's entire history (spec: "Do not repeatedly re-download and
# re-analyze the entire mailbox/history").
INITIAL_SYNC_LOOKBACK_DAYS = 7

# Re-fetch a small overlap behind the last checkpoint so a message that
# lands mid-sync (received_at just before start_at, indexed by the
# provider slightly late) is never silently skipped — upsert-by-
# provider-message-id makes re-seeing it a safe no-op, never a duplicate.
SYNC_OVERLAP_BUFFER = timedelta(minutes=5)


SPAM_OR_AD_KEYWORDS = [
    "unsubscribe",
    "limited time offer",
    "discount",
    "sale",
    "promo",
    "promotion",
    "advertisement",
    "sponsored",
    "newsletter",
    "marketing",
    "deal",
    "coupon",
    "buy now",
    "free trial",
    "webinar invitation",
    "do not reply",
    "noreply",
    "no-reply",
]


def ensure_aware_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(timezone.utc)


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None

    raw = value.strip()

    if raw.endswith("Z"):
        raw = raw.replace("Z", "+00:00")

    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None

    return ensure_aware_utc(parsed)


def parse_graph_datetime(value: dict | None) -> datetime | None:
    if not value:
        return None

    raw = value.get("dateTime")

    if not raw:
        return None

    raw = str(raw).strip()

    if raw.endswith("Z"):
        raw = raw.replace("Z", "+00:00")

    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None

    return ensure_aware_utc(parsed)


def parse_transcript_datetime(transcript: dict | None) -> datetime | None:
    if not transcript:
        return None

    possible_keys = [
        "createdDateTime",
        "lastModifiedDateTime",
        "startDateTime",
        "endDateTime",
    ]

    for key in possible_keys:
        value = transcript.get(key)

        if value:
            parsed = parse_iso_datetime(value)

            if parsed:
                return parsed

    return None


def is_datetime_in_range(
    value: datetime | None,
    start_at: datetime,
    end_at: datetime,
) -> bool:
    value = ensure_aware_utc(value)
    start_at = ensure_aware_utc(start_at)
    end_at = ensure_aware_utc(end_at)

    if not value or not start_at or not end_at:
        return False

    return start_at <= value < end_at


def normalize_text(value: str | None) -> str:
    return (value or "").strip().lower()


def is_probably_non_task_email(
    subject: str | None,
    body: str | None,
    sender: str | None = None,
) -> bool:
    """Smart action-item detection follow-up (bug fix): this used to be a
    plain substring check (`keyword in text`), which false-positived on
    any word merely CONTAINING a keyword as a substring — e.g. "sale" is
    a substring of "sales" ("prepare the monthly SALES report"),
    "promo" of "promotion" is a keyword itself already but "personal" a
    substring inside "personalized", etc. A genuinely actionable email
    could be silently discarded before the AI ever saw it. Fixed to
    match whole words only (regex `\\b` boundaries), same keyword list."""
    text = f"{subject or ''} {body or ''} {sender or ''}".lower()
    return any(re.search(rf"\b{re.escape(keyword)}\b", text) for keyword in SPAM_OR_AD_KEYWORDS)


def get_yesterday_range_utc() -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)

    today_start = datetime(
        year=now.year,
        month=now.month,
        day=now.day,
        tzinfo=timezone.utc,
    )

    yesterday_start = today_start - timedelta(days=1)

    return yesterday_start, now


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


def resolve_user_id(candidates: list[User], name: str | None, email: str | None) -> int | None:
    """Name/email resolution — SECURITY-SENSITIVE: `candidates` is trusted
    as-is with no further tenant/team filtering here, so every caller MUST
    already have scoped it (organization-safe candidate set, and further
    narrowed to Team members for a Team Task) before calling this. Never
    pass UserRepository.list_all() or any other unscoped/global user list."""
    users = candidates
    normalized_email = normalize_text(email)
    normalized_name = normalize_text(name)

    if normalized_email:
        for user in users:
            if normalize_text(user.email) == normalized_email:
                return user.id

    if normalized_name:
        # Exact match first
        for user in users:
            if normalize_text(user.full_name) == normalized_name:
                return user.id
        # Partial match: extracted name is part of full name or vice versa
        for user in users:
            full = normalize_text(user.full_name)
            if normalized_name in full or full in normalized_name:
                return user.id

    return None


def resolve_project_id(projects: list[Project], project_name: str | None) -> int | None:
    normalized_project_name = normalize_text(project_name)

    if not normalized_project_name:
        return None

    for project in projects:
        if normalize_text(project.name) == normalized_project_name:
            return project.id

    return None


def resolve_team_id(teams: list[Team], team_name: str | None) -> int | None:
    normalized_team_name = normalize_text(team_name)

    if not normalized_team_name:
        return None

    for team in teams:
        if normalize_text(team.name) == normalized_team_name:
            return team.id

    return None


_TITLE_STOPWORDS = {
    "the", "and", "for", "with", "team", "group",
    "meeting", "call", "sync", "review", "session",
    "a", "an", "of", "in", "on", "at", "to",
}


def resolve_team_from_meeting_title(teams: list[Team], title: str | None) -> int | None:
    if not title:
        return None

    normalized_title = normalize_text(title)
    title_words = {w for w in normalized_title.split() if len(w) >= 3} - _TITLE_STOPWORDS

    for team in teams:
        team_name = normalize_text(team.name)
        if not team_name:
            continue

        # Full team name is a substring of the title (e.g. "marketing" in "marketing strategy meeting")
        if team_name in normalized_title:
            return team.id

        # Any significant word from the team name appears in the title
        team_words = {w for w in team_name.split() if len(w) >= 3} - _TITLE_STOPWORDS
        if team_words and team_words & title_words:
            return team.id

    return None


def find_fallback_assignee_id(
    candidates: list[tuple[User, str]],
    fallback_email: str | None,
    integration_owner_id: int,
    eligible_ids: set[int] | None = None,
) -> int | None:
    """Tenant/team-safe fallback-assignee heuristic (cross-tenant
    automation-assignee security fix).

    `candidates` MUST already be scoped to the current organization's
    active, non-Client members — see
    `UserRepository.list_org_assignable_candidates()` — as (User, org_role)
    pairs, `org_role` being `OrganizationMembership.role` for THIS
    organization, never the legacy/global `User.role` column (a user who
    is Admin in another org, or whose stale `User.role` merely SAYS
    "admin", must never be treated as this org's Admin fallback).

    `eligible_ids`, when given (a Team Task), further restricts every step
    below to members of that exact team — the same Rule B
    `app.core.task_assignment.validate_task_assignee` enforces at
    persistence time. An organization Admin who is not a member of the
    task's team must NOT become its fallback assignee merely by being
    Admin (Team eligibility is the boundary, not org-wide privilege).

    Preserves the original 3-step intent exactly — source owner, then
    first Admin, then the integration owner — just made tenant/team-safe
    and FAIL-SAFE: if no step finds an eligible candidate, returns None
    (Unassigned) instead of unconditionally trusting an id that may not
    actually be eligible (the old code always returned
    `integration_owner_id` as an unconditional last resort, even if that
    id somehow wasn't a valid candidate — see PHASE 12).

    Determinism (PHASE 20): "first Admin" is the first Admin encountered
    in `candidates` — callers pass it pre-sorted by full_name ascending
    (same ordering `list_org_assignable_candidates()`/
    `list_assignable_members()` already use), never undefined DB row
    order.
    """
    def is_eligible(user_id: int) -> bool:
        return eligible_ids is None or user_id in eligible_ids

    candidate_ids = {u.id for u, _role in candidates}

    # 1. Try the source owner (meeting organizer or email sender) — resolved
    # only among the already-scoped candidate set, so an email belonging to
    # someone outside this organization/team can never match here.
    if fallback_email:
        owner_id = resolve_user_id([u for u, _role in candidates], None, fallback_email)
        if owner_id is not None and is_eligible(owner_id):
            return owner_id

    # 2. Fall back to the first Admin, per THIS organization's authoritative
    # OrganizationMembership.role.
    for u, role in candidates:
        if role == ADMIN and is_eligible(u.id):
            return u.id

    # 3. Last resort: the user who owns the integration/triggered this sync
    # — only if they are themselves still an eligible candidate (active,
    # non-Client member of this org, and of this task's team if scoped).
    if integration_owner_id in candidate_ids and is_eligible(integration_owner_id):
        return integration_owner_id

    # 4. No safe fallback within this organization/team scope — leave the
    # Task unassigned rather than ever reaching outside the scope this
    # function was given.
    return None


async def resolve_org_id_for_user(db: AsyncSession, user: User):
    """Best-effort org context for background jobs that iterate over users
    system-wide (no request-scoped TenantContext available). Prefers the
    user's last-selected org, falling back to their oldest active membership.
    Returns None if the user has no org at all."""
    if user.last_active_organization_id:
        return user.last_active_organization_id

    result = await db.execute(
        select(OrganizationMembership.organization_id)
        .where(OrganizationMembership.user_id == user.id, OrganizationMembership.is_active.is_(True))
        .order_by(OrganizationMembership.created_at.asc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def create_sync_run(
    db: AsyncSession,
    *,
    organization_id,
    provider: str,
    integration_account_id: int | None,
    triggered_by_user_id: int | None,
    trigger: str,
) -> SyncRun:
    """Automation Pipeline Audit follow-up — the persistent Sync Run
    record. Created BEFORE any fetch/analysis work starts so a crash mid-
    run still leaves a "running" (then, via finish_sync_run, "failed")
    row behind instead of the attempt vanishing entirely."""
    run = SyncRun(
        organization_id=organization_id,
        provider=provider,
        integration_account_id=integration_account_id,
        triggered_by_user_id=triggered_by_user_id,
        trigger=trigger,
        status=SYNC_RUN_RUNNING,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    logger.info(
        "sync_run started",
        extra={"sync_run_id": run.id, "organization_id": str(organization_id), "provider": provider, "trigger": trigger},
    )
    return run


async def finish_sync_run(
    db: AsyncSession,
    run: SyncRun,
    *,
    fatal_error_code: str | None = None,
    fatal_error_message: str | None = None,
) -> SyncRun:
    """Finalizes a Sync Run's status from its own accumulated counters —
    never a caller-guessed "it worked" flag. success: no failures at all.
    partial_success: some failures, but something also actually
    completed (fetched/analyzed/created something). failed: a fatal
    error before any useful work, or failures with nothing else to show
    for the run."""
    run.completed_at = datetime.now(timezone.utc)
    if fatal_error_code:
        run.status = SYNC_RUN_FAILED
        run.last_error_code = fatal_error_code
        run.last_error_message = fatal_error_message or safe_error_message(fatal_error_code)
    elif run.failed_count == 0:
        run.status = SYNC_RUN_SUCCESS
    elif run.fetched_count > 0 or run.analyzed_count > 0 or run.tasks_created_count > 0:
        run.status = SYNC_RUN_PARTIAL_SUCCESS
    else:
        run.status = SYNC_RUN_FAILED
    await db.commit()
    await db.refresh(run)
    logger.info(
        "sync_run finished",
        extra={
            "sync_run_id": run.id, "status": run.status,
            "fetched_count": run.fetched_count, "analyzed_count": run.analyzed_count,
            "tasks_created_count": run.tasks_created_count, "failed_count": run.failed_count,
        },
    )
    return run


def _org_local_today(org: Organization | None) -> date:
    """Due-date/timezone follow-up: the organization's own authoritative
    timezone (Organization.timezone, defaults to 'UTC') decides what
    "today" means for resolving relative phrases like "Friday" — never
    the automation worker process's own local/UTC date, which has no
    relationship to where the organization actually operates."""
    tz_name = getattr(org, "timezone", None) or "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")
    return datetime.now(tz).date()


async def sync_microsoft_data_for_user(
    *,
    db: AsyncSession,
    user: User,
    sync_run: SyncRun | None = None,
) -> dict:
    """Microsoft adapter: FETCH + NORMALIZE stage only (see the module
    docstring in app.core.automation_pipeline) — writes ImportedEmail /
    CalendarEvent / MeetingTranscript rows and returns. AI analysis and
    Task creation happen in analyze_pending_sources_for_user(), the same
    shared function Gmail's adapter feeds into.

    Incremental sync (spec: "do not repeatedly re-download and re-analyze
    the entire mailbox/history"): each account's own `last_synced_at`
    checkpoint is the fetch window's start, with a small overlap buffer;
    a brand-new account bootstraps from INITIAL_SYNC_LOOKBACK_DAYS rather
    than importing all history. The checkpoint only advances on an
    account whose fetch stage completed without a fatal error — a failed
    fetch must never skip over unprocessed data on the next attempt.
    """
    org_id = await resolve_org_id_for_user(db, user)
    empty_result = {
        "emails_imported": 0, "calendar_events_imported": 0,
        "transcripts_imported": 0, "transcript_errors": [],
    }
    if org_id is None:
        return empty_result

    repository = IntegrationRepository(db, org_id)
    accounts = await repository.list_accounts_by_provider(user.id, "microsoft")
    if not accounts:
        return empty_result

    now = ensure_aware_utc(datetime.now(timezone.utc))
    total_emails = 0
    total_events = 0
    total_transcripts = 0
    transcript_errors: list[dict] = []

    for account in accounts:
        checkpoint = ensure_aware_utc(account.last_synced_at)
        start_at = (checkpoint - SYNC_OVERLAP_BUFFER) if checkpoint else (now - timedelta(days=INITIAL_SYNC_LOOKBACK_DAYS))
        end_at = now

        logger.info(
            "microsoft sync account start",
            extra={
                "provider": "microsoft", "stage": "fetch", "status": "running",
                "integration_account_id": account.id, "user_id": user.id,
                "organization_id": str(org_id), "window_start": start_at.isoformat(), "window_end": end_at.isoformat(),
            },
        )

        account_fetch_failed = False

        try:
            await MicrosoftGraphService(account).ensure_fresh_token(db)
        except Exception as exc:
            error_code = classify_http_error(exc)
            logger.warning(
                "microsoft token refresh failed",
                extra={"provider": "microsoft", "stage": "token_refresh", "status": "failed", "integration_account_id": account.id, "error_code": error_code},
            )
            account.last_sync_status = "failed"
            account.last_sync_error = safe_error_message(error_code)
            await db.commit()
            if sync_run is not None:
                sync_run.failed_count += 1
            continue

        graph = MicrosoftGraphService(account)

        try:
            messages = await graph.list_messages(start_at, end_at)
        except Exception as exc:
            error_code = classify_http_error(exc)
            logger.warning(
                "microsoft email fetch failed",
                extra={"provider": "microsoft", "stage": "email_fetch", "status": "failed", "integration_account_id": account.id, "error_code": error_code},
            )
            messages = []
            account_fetch_failed = True
            if sync_run is not None:
                sync_run.failed_count += 1

        for message in messages:
            received_at = parse_iso_datetime(message.get("receivedDateTime"))
            body = message.get("body") or {}
            sender = extract_email_address(message.get("from"))
            recipients = extract_recipients(message.get("toRecipients"))

            await repository.upsert_imported_email(
                integration_account_id=account.id,
                provider_message_id=message["id"],
                subject=message.get("subject"),
                sender=sender,
                recipients=recipients,
                received_at=received_at,
                snippet=message.get("bodyPreview"),
                body_text=body.get("content") or message.get("bodyPreview"),
                raw_payload=message,
            )
            total_emails += 1

        try:
            events = await graph.list_calendar_events(start_at, end_at)
        except Exception as exc:
            error_code = classify_http_error(exc)
            logger.warning(
                "microsoft calendar fetch failed",
                extra={"provider": "microsoft", "stage": "calendar_fetch", "status": "failed", "integration_account_id": account.id, "error_code": error_code},
            )
            events = []
            account_fetch_failed = True
            if sync_run is not None:
                sync_run.failed_count += 1

        for event in events:
            event_starts_at = parse_graph_datetime(event.get("start"))
            event_ends_at = parse_graph_datetime(event.get("end"))
            organizer = event.get("organizer") or {}
            organizer_email = extract_email_address(organizer)
            attendees = [
                {
                    "email": extract_email_address(attendee),
                    "name": (attendee.get("emailAddress") or {}).get("name"),
                    "type": attendee.get("type"),
                }
                for attendee in (event.get("attendees") or [])
            ]
            online_meeting = event.get("onlineMeeting") or {}
            meeting_url = event.get("onlineMeetingUrl") or online_meeting.get("joinUrl") or event.get("webLink")

            saved_event = await repository.upsert_calendar_event(
                integration_account_id=account.id,
                provider_event_id=event["id"],
                title=event.get("subject"),
                organizer_email=organizer_email,
                attendees=attendees,
                starts_at=event_starts_at,
                ends_at=event_ends_at,
                meeting_url=meeting_url,
                provider="microsoft",
                raw_payload=event,
            )
            total_events += 1

            online_meeting_id = online_meeting.get("id")
            if not online_meeting_id and meeting_url:
                try:
                    found_meeting = await graph.find_online_meeting_by_join_url(meeting_url)
                    online_meeting_id = found_meeting.get("id") if found_meeting else None
                except Exception as exc:
                    transcript_errors.append({
                        "event_id": event.get("id"), "title": event.get("subject"),
                        "stage": "resolve_online_meeting", "error_code": classify_http_error(exc),
                    })

            if not online_meeting_id:
                saved_event.transcript_status = TRANSCRIPT_STATUS_NOT_SUPPORTED
                await db.commit()
                continue

            # Teams transcript handling follow-up: this same event can be
            # re-seen on every sync tick for as long as it stays inside
            # the incremental window's overlap buffer (see
            # SYNC_OVERLAP_BUFFER) — an event already on a scheduled
            # capped-backoff retry (`waiting`, next_check_at in the
            # future) must NOT be re-checked here too, or the backoff
            # schedule is meaningless and this degrades into exactly the
            # "poll forever" pattern the spec forbids. Already-terminal
            # states (available/permission_denied/expired) also skip —
            # _recheck_waiting_transcripts below is the only path that
            # re-examines a "waiting" event, and only once its own
            # schedule says it's due.
            already_tracked = saved_event.transcript_status not in (TRANSCRIPT_STATUS_UNKNOWN, TRANSCRIPT_STATUS_NOT_SUPPORTED)
            if already_tracked:
                continue

            imported = await _fetch_and_track_transcripts(
                db=db, repository=repository, graph=graph, event=saved_event, online_meeting_id=online_meeting_id,
                transcript_errors=transcript_errors,
            )
            total_transcripts += imported

        # Meetings discovered in an EARLIER run may have just become
        # ready ("waiting_for_transcript... retry later using capped
        # backoff") — re-check those due for a retry now, independent of
        # this run's own fetch window.
        total_transcripts += await _recheck_waiting_transcripts(
            db=db, repository=repository, graph=graph, account_id=account.id, now=now, transcript_errors=transcript_errors,
        )

        if not account_fetch_failed:
            account.last_synced_at = end_at
            account.last_sync_status = "success"
            account.last_sync_error = None
        else:
            account.last_sync_status = "failed"
            account.last_sync_error = safe_error_message(ERROR_PROVIDER_UNAVAILABLE)
        await db.commit()

        logger.info(
            "microsoft sync account done",
            extra={
                "provider": "microsoft", "stage": "fetch", "status": "failed" if account_fetch_failed else "success",
                "integration_account_id": account.id, "emails": len(messages), "events": len(events),
            },
        )

    if sync_run is not None:
        sync_run.fetched_count += total_emails + total_events + total_transcripts

    return {
        "emails_imported": total_emails,
        "calendar_events_imported": total_events,
        "transcripts_imported": total_transcripts,
        "transcript_errors": transcript_errors,
    }


async def sync_gmail_data_for_user(
    *,
    db: AsyncSession,
    user: User,
    sync_run: SyncRun | None = None,
) -> dict:
    """Gmail adapter (Automation Pipeline Audit follow-up, Phase 2) —
    FETCH + NORMALIZE stage only, mirroring sync_microsoft_data_for_user
    exactly (same incremental-checkpoint strategy, same
    last_synced_at-only-advances-on-success rule, same
    IntegrationRepository.upsert_imported_email() write path). AI
    analysis and Task creation happen in the SAME shared
    analyze_pending_sources_for_user() Microsoft already uses — Gmail
    emails and Outlook emails land in the identical ImportedEmail table
    and are indistinguishable to that function except by
    `integration_account.provider`.

    Gmail has no Teams-equivalent meeting/transcript surface, so this
    adapter never touches CalendarEvent/MeetingTranscript at all."""
    from app.services.integrations.gmail import GmailService

    org_id = await resolve_org_id_for_user(db, user)
    empty_result = {"emails_imported": 0, "transcript_errors": []}
    if org_id is None:
        return empty_result

    repository = IntegrationRepository(db, org_id)
    accounts = await repository.list_accounts_by_provider(user.id, "google")
    if not accounts:
        return empty_result

    now = ensure_aware_utc(datetime.now(timezone.utc))
    total_emails = 0

    for account in accounts:
        checkpoint = ensure_aware_utc(account.last_synced_at)
        start_at = (checkpoint - SYNC_OVERLAP_BUFFER) if checkpoint else (now - timedelta(days=INITIAL_SYNC_LOOKBACK_DAYS))
        end_at = now

        logger.info(
            "gmail sync account start",
            extra={
                "provider": "google", "stage": "fetch", "status": "running",
                "integration_account_id": account.id, "user_id": user.id,
                "organization_id": str(org_id), "window_start": start_at.isoformat(), "window_end": end_at.isoformat(),
            },
        )

        gmail = GmailService(account)
        account_fetch_failed = False

        try:
            await gmail.ensure_fresh_token(db)
            messages = await gmail.list_messages(start_at, end_at)
        except Exception as exc:
            error_code = classify_http_error(exc)
            logger.warning(
                "gmail fetch failed",
                extra={"provider": "google", "stage": "email_fetch", "status": "failed", "integration_account_id": account.id, "error_code": error_code},
            )
            messages = []
            account_fetch_failed = True
            if sync_run is not None:
                sync_run.failed_count += 1

        for message in messages:
            body = message.get("body") or {}
            await repository.upsert_imported_email(
                integration_account_id=account.id,
                provider_message_id=message["id"],
                subject=message.get("subject"),
                sender=extract_email_address(message.get("from")),
                recipients=extract_recipients(message.get("toRecipients")),
                received_at=parse_iso_datetime(message.get("receivedDateTime")),
                snippet=message.get("bodyPreview"),
                body_text=body.get("content") or message.get("bodyPreview"),
                raw_payload=message,
            )
            total_emails += 1

        if not account_fetch_failed:
            account.last_synced_at = end_at
            account.last_sync_status = "success"
            account.last_sync_error = None
        else:
            account.last_sync_status = "failed"
            account.last_sync_error = safe_error_message(ERROR_PROVIDER_UNAVAILABLE)
        await db.commit()

        logger.info(
            "gmail sync account done",
            extra={
                "provider": "google", "stage": "fetch", "status": "failed" if account_fetch_failed else "success",
                "integration_account_id": account.id, "emails": len(messages),
            },
        )

    if sync_run is not None:
        sync_run.fetched_count += total_emails

    return {"emails_imported": total_emails, "transcript_errors": []}


async def _fetch_and_track_transcripts(
    *, db: AsyncSession, repository: IntegrationRepository, graph: MicrosoftGraphService,
    event: CalendarEvent, online_meeting_id: str, transcript_errors: list[dict],
) -> int:
    """Teams transcript handling follow-up: a meeting ending does NOT
    guarantee the transcript is instantly available. If Graph returns no
    transcripts yet, this records `waiting_for_transcript` with a capped-
    backoff `transcript_next_check_at` (see
    app.core.automation_pipeline.TRANSCRIPT_RETRY_BACKOFF_MINUTES) rather
    than treating it as a permanent failure or polling forever."""
    imported = 0
    try:
        transcripts = await graph.list_transcripts_for_online_meeting(online_meeting_id)
    except Exception as exc:
        error_code = classify_http_error(exc)
        transcript_errors.append({"event_id": event.provider_event_id, "title": event.title, "stage": "list_transcripts", "error_code": error_code})
        if error_code == ERROR_TRANSCRIPT_PERMISSION_DENIED:
            event.transcript_status = TRANSCRIPT_STATUS_PERMISSION_DENIED
        else:
            event.transcript_attempts += 1
            event.transcript_status = TRANSCRIPT_STATUS_WAITING
            event.transcript_next_check_at = datetime.now(timezone.utc) + timedelta(minutes=transcript_backoff_minutes(event.transcript_attempts))
        await db.commit()
        return 0

    if not transcripts:
        event.transcript_attempts += 1
        if event.transcript_attempts >= 5:  # TRANSCRIPT_MAX_ATTEMPTS
            event.transcript_status = "expired"
            logger.info(
                "transcript retry budget exhausted",
                extra={"provider": "microsoft", "stage": "transcript_fetch", "status": "expired", "event_id": event.provider_event_id, "attempts": event.transcript_attempts},
            )
        else:
            event.transcript_status = TRANSCRIPT_STATUS_WAITING
            event.transcript_next_check_at = datetime.now(timezone.utc) + timedelta(minutes=transcript_backoff_minutes(event.transcript_attempts))
            logger.info(
                "transcript not ready",
                extra={"provider": "microsoft", "stage": "transcript_fetch", "status": "waiting", "event_id": event.provider_event_id, "attempts": event.transcript_attempts},
            )
        await db.commit()
        return 0

    for transcript in transcripts:
        transcript_id = transcript.get("id")
        if not transcript_id:
            continue
        try:
            transcript_text = await graph.get_transcript_content(online_meeting_id, transcript_id)
        except Exception as exc:
            transcript_errors.append({"event_id": event.provider_event_id, "title": event.title, "stage": "get_transcript_content", "error_code": classify_http_error(exc)})
            continue
        if not transcript_text:
            continue

        await repository.upsert_meeting_transcript(
            calendar_event_id=event.id,
            provider_transcript_id=transcript_id,
            transcript_text=transcript_text,
            raw_payload=transcript,
        )
        imported += 1

    event.transcript_status = TRANSCRIPT_STATUS_AVAILABLE if imported else event.transcript_status
    await db.commit()
    logger.info(
        "transcript fetch done",
        extra={"provider": "microsoft", "stage": "transcript_fetch", "status": "success" if imported else "empty", "event_id": event.provider_event_id, "transcripts_imported": imported},
    )
    return imported


async def _recheck_waiting_transcripts(
    *, db: AsyncSession, repository: IntegrationRepository, graph: MicrosoftGraphService,
    account_id: int, now: datetime, transcript_errors: list[dict],
) -> int:
    result = await db.execute(
        select(CalendarEvent).where(
            CalendarEvent.integration_account_id == account_id,
            CalendarEvent.transcript_status == TRANSCRIPT_STATUS_WAITING,
            CalendarEvent.transcript_next_check_at.is_not(None),
            CalendarEvent.transcript_next_check_at <= now,
        )
    )
    due_events = list(result.scalars().all())
    imported_total = 0
    for event in due_events:
        raw = event.raw_payload or {}
        online_meeting = raw.get("onlineMeeting") or {}
        online_meeting_id = online_meeting.get("id")
        if not online_meeting_id and event.meeting_url:
            try:
                found = await graph.find_online_meeting_by_join_url(event.meeting_url)
                online_meeting_id = found.get("id") if found else None
            except Exception as exc:
                transcript_errors.append({"event_id": event.provider_event_id, "title": event.title, "stage": "resolve_online_meeting_recheck", "error_code": classify_http_error(exc)})
        if not online_meeting_id:
            event.transcript_status = TRANSCRIPT_STATUS_NOT_SUPPORTED
            await db.commit()
            continue
        imported_total += await _fetch_and_track_transcripts(
            db=db, repository=repository, graph=graph, event=event, online_meeting_id=online_meeting_id,
            transcript_errors=transcript_errors,
        )
    return imported_total


async def _claim_source_rows(db: AsyncSession, model, user_id: int, *, account_join, limit: int = 200) -> list:
    """Idempotency/concurrency follow-up — the actual guard against two
    overlapping sync runs (manual + scheduled, two app instances, a
    retried request) double-processing the same email/transcript and
    creating duplicate Tasks.

    `UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED) RETURNING`
    is a single atomic statement: two concurrent callers running this at
    the same moment can never claim the same row — the second caller's
    inner SELECT skips whatever the first has already row-locked, so the
    two claims are always disjoint sets. This is the database-constraint-
    grade protection the spec asks for, not a plain
    `if already_processed: return` race."""
    id_col = model.id
    subquery = (
        account_join(select(id_col))
        .where(IntegrationAccount.user_id == user_id, model.processing_status == SOURCE_STATUS_DISCOVERED)
        .limit(limit)
        .with_for_update(of=model, skip_locked=True)
    )
    stmt = (
        sql_update(model)
        .where(id_col.in_(subquery))
        .values(processing_status=SOURCE_STATUS_ANALYZING)
        .returning(id_col)
    )
    result = await db.execute(stmt)
    claimed_ids = [row[0] for row in result.all()]
    await db.commit()
    if not claimed_ids:
        return []
    rows = await db.execute(select(model).where(id_col.in_(claimed_ids)))
    return list(rows.scalars().unique().all())


async def analyze_pending_sources_for_user(
    *,
    db: AsyncSession,
    user: User,
    org_id,
    sync_run: SyncRun | None = None,
) -> dict:
    """Shared, provider-neutral AI-analysis + Task-creation stage (see the
    module docstring in app.core.automation_pipeline). Operates on
    whatever `ImportedEmail`/`MeetingTranscript` rows are sitting in
    processing_status="discovered" for this user, REGARDLESS of which
    provider adapter (Microsoft or Gmail) wrote them — both write into
    the same tables via the same IntegrationRepository upserts, so this
    function never needs a per-provider branch.

    Renamed from analyze_yesterday_sources_for_user(): it no longer
    operates on a fixed "yesterday" window at all — it claims whatever is
    pending, which is what actually made it idempotent-safe to call
    repeatedly/concurrently (see _claim_source_rows).
    """
    extractor = AITaskExtractor()
    task_repo = TaskRepository(db, org_id)
    user_repo = UserRepository(db)
    project_repo = ProjectRepository(db, org_id)
    team_repo = TeamRepository(db, org_id)

    org_result = await db.execute(select(Organization).where(Organization.id == org_id))
    organization = org_result.scalar_one_or_none()
    reference_date = _org_local_today(organization)

    # SECURITY: org-scoped candidate set only (cross-tenant automation-
    # assignee fix) — NEVER user_repo.list_all(), which scans every user in
    # the entire database with no organization boundary. Active, non-Client
    # members of THIS organization only; carries each user's authoritative
    # OrganizationMembership.role (never the legacy/global User.role) for
    # the Admin-fallback check below.
    user_candidates = await user_repo.list_org_assignable_candidates(org_id)
    projects = await project_repo.list_all()
    teams = await team_repo.list_all()

    def _email_account_join(q):
        return q.join(IntegrationAccount, ImportedEmail.integration_account_id == IntegrationAccount.id)

    def _transcript_account_join(q):
        return (
            q.join(CalendarEvent, MeetingTranscript.calendar_event_id == CalendarEvent.id)
            .join(IntegrationAccount, CalendarEvent.integration_account_id == IntegrationAccount.id)
        )

    emails = await _claim_source_rows(db, ImportedEmail, user.id, account_join=_email_account_join)
    transcripts = await _claim_source_rows(db, MeetingTranscript, user.id, account_join=_transcript_account_join)
    if transcripts:
        # calendar_event is needed for meeting title/organizer below —
        # refresh with the relationship eager-loaded (the claim query
        # above only needs the id, so this is a small, separate fetch).
        transcript_ids = [t.id for t in transcripts]
        result = await db.execute(
            select(MeetingTranscript)
            .where(MeetingTranscript.id.in_(transcript_ids))
            .options(selectinload(MeetingTranscript.calendar_event))
        )
        transcripts = list(result.scalars().all())

    sources_analyzed = 0
    tasks_created = 0
    tasks_created_without_assignee = 0
    tasks_created_without_project = 0
    tasks_created_without_team = 0
    tasks_created_without_start_date = 0
    tasks_created_without_due_date = 0
    tasks_needing_review = 0
    emails_skipped_as_non_task = 0
    emails_without_text = 0
    sources_failed = 0

    async def handle_extracted_tasks(
        *,
        source_row,
        source_type: str,
        provenance_source_type: str,
        source_title: str | None,
        source_text: str,
        source_external_id: str,
        provider: str,
        source_date: datetime | None = None,
        fallback_assignee_email: str | None = None,
    ) -> str:
        """Runs AI analysis for one source row and returns its final
        processing_status. Never raises — a failure here becomes
        SOURCE_STATUS_FAILED with a safe error message rather than
        aborting the whole batch over one bad item."""
        nonlocal sources_analyzed, tasks_created, tasks_created_without_assignee
        nonlocal tasks_created_without_project, tasks_created_without_team
        nonlocal tasks_created_without_start_date, tasks_created_without_due_date
        nonlocal tasks_needing_review

        # Privacy (PHASE 21) + tenant safety: the AI extractor's
        # "known users" hint list is built from the org-scoped candidate
        # set only — never leaks another organization's user names/emails
        # into this org's extraction prompt.
        known_users_list = [{"name": u.full_name, "email": u.email} for u, _role in user_candidates]

        try:
            extracted_tasks, raw_payload = await extractor.extract_tasks(
                source_type=source_type,
                source_title=source_title,
                source_text=source_text,
                known_users=known_users_list,
                reference_date=reference_date,
            )
        except Exception:
            logger.exception(
                "ai analysis raised unexpectedly",
                extra={"provider": provider, "stage": "ai_analysis", "status": "failed", "source_type": source_type, "source_external_id": source_external_id},
            )
            source_row.last_error = safe_error_message(ERROR_AI_ANALYSIS_FAILED)
            return SOURCE_STATUS_FAILED

        sources_analyzed += 1
        source_row.ai_result_summary = (raw_payload.get("reason") or "")[:500] or None

        logger.info(
            "ai analysis done",
            extra={
                "provider": provider, "stage": "ai_analysis", "status": "success",
                "source_type": source_type, "source_external_id": source_external_id,
                "action_items": len(extracted_tasks), "category": raw_payload.get("source_category"),
            },
        )

        if not extracted_tasks:
            return SOURCE_STATUS_NO_ACTION_REQUIRED

        created_any = False
        review_any = False

        for extracted_task in extracted_tasks:
            if not meets_auto_create_bar(extracted_task.confidence):
                # MEDIUM/ambiguous or LOW confidence: never auto-create —
                # visible in Automation Activity as needs_review instead
                # (spec's "CONFIDENCE + HUMAN REVIEW").
                review_any = True
                tasks_needing_review += 1
                continue

            # Team resolution happens BEFORE assignee resolution (PHASE 13/
            # 14) — a Team Task's assignee boundary is that exact Team, so
            # the candidate set for both explicit name/email matching and
            # fallback selection must already be narrowed to the team
            # before either runs, never widened back to the whole org.
            team_id = resolve_team_id(teams, extracted_task.suggested_team_name)
            if team_id is None and source_type == "transcript":
                team_id = resolve_team_from_meeting_title(teams, source_title)

            if team_id is not None:
                team_members = await team_repo.list_assignable_members(team_id)
                assignee_pool = team_members
                eligible_ids = {u.id for u in team_members}
            else:
                assignee_pool = [u for u, _role in user_candidates]
                eligible_ids = None

            assignee_id = resolve_user_id(assignee_pool, extracted_task.suggested_assignee_name, extracted_task.suggested_assignee_email)
            if assignee_id is None:
                assignee_id = find_fallback_assignee_id(user_candidates, fallback_assignee_email, user.id, eligible_ids=eligible_ids)
                tasks_created_without_assignee += 1

            project_id = resolve_project_id(projects, extracted_task.suggested_project_name)
            if project_id is None:
                tasks_created_without_project += 1
            if team_id is None:
                tasks_created_without_team += 1
            if extracted_task.suggested_start_date is None:
                tasks_created_without_start_date += 1
            if extracted_task.suggested_due_date is None:
                tasks_created_without_due_date += 1

            try:
                task = await task_repo.create(
                    TaskCreate(
                        name=extracted_task.title,
                        description=extracted_task.description,
                        start_date=extracted_task.suggested_start_date,
                        due_date=extracted_task.suggested_due_date,
                        assignee_id=assignee_id,
                        project_id=project_id,
                        team_id=team_id,
                        status="todo",
                    ),
                    created_by_id=user.id,
                )
            except AppException:
                # Defense-in-depth (PHASE 16): TaskRepository.create() now
                # itself enforces validate_task_assignee() before
                # persisting, so a resolution defect above can never reach
                # the database with an invalid/cross-tenant/Client/inactive
                # assignee — it fails safe to Unassigned instead of
                # crashing the whole batch over one bad candidate.
                logger.warning(
                    "automation-resolved assignee failed final validation — creating unassigned instead",
                    extra={"provider": provider, "stage": "task_creation", "organization_id": str(org_id)},
                )
                try:
                    task = await task_repo.create(
                        TaskCreate(
                            name=extracted_task.title,
                            description=extracted_task.description,
                            start_date=extracted_task.suggested_start_date,
                            due_date=extracted_task.suggested_due_date,
                            assignee_id=None,
                            project_id=project_id,
                            team_id=team_id,
                            status="todo",
                        ),
                        created_by_id=user.id,
                    )
                except AppException:
                    logger.exception(
                        "task creation failed even unassigned",
                        extra={"provider": provider, "stage": "task_creation", "status": "failed", "organization_id": str(org_id)},
                    )
                    review_any = True
                    tasks_needing_review += 1
                    continue
                tasks_created_without_assignee += 1

            # Source -> Task provenance: exactly one TaskSource row per
            # created Task, safe display metadata only (never the email
            # body / transcript text, which stay on the source row).
            db.add(TaskSource(
                task_id=task.id,
                organization_id=org_id,
                provider=provider,
                source_type=provenance_source_type,
                source_external_id=source_external_id,
                integration_account_id=getattr(source_row, "integration_account_id", None),
                sync_run_id=sync_run.id if sync_run is not None else None,
                ai_generated=True,
                confidence=extracted_task.confidence,
                source_title=(source_title or "")[:500] or None,
                source_date=source_date,
            ))
            await db.commit()

            tasks_created += 1
            created_any = True

        if created_any and review_any:
            return SOURCE_STATUS_PARTIALLY_CREATED
        if created_any:
            return SOURCE_STATUS_TASK_CREATED
        if review_any:
            return SOURCE_STATUS_NEEDS_REVIEW
        return SOURCE_STATUS_NO_ACTION_REQUIRED

    for email in emails:
        provider = (email.integration_account.provider if email.integration_account else "microsoft")
        source_type_label = "gmail_email" if provider == "google" else "microsoft_outlook_email"
        source_text = email.body_text or email.snippet

        if not source_text:
            emails_without_text += 1
            email.processing_status = SOURCE_STATUS_NO_ACTION_REQUIRED
            await db.commit()
            continue

        if is_probably_non_task_email(email.subject, source_text, email.sender):
            emails_skipped_as_non_task += 1
            email.processing_status = SOURCE_STATUS_NO_ACTION_REQUIRED
            await db.commit()
            continue

        final_status = await handle_extracted_tasks(
            source_row=email,
            source_type="email",
            provenance_source_type=source_type_label,
            source_title=email.subject,
            source_text=source_text,
            source_external_id=email.provider_message_id,
            provider=provider,
            source_date=ensure_aware_utc(email.received_at),
            fallback_assignee_email=email.sender,
        )
        email.processing_status = final_status
        if final_status == SOURCE_STATUS_FAILED:
            sources_failed += 1
        await db.commit()

    for transcript in transcripts:
        calendar_event = transcript.calendar_event
        provider = (calendar_event.integration_account.provider if calendar_event and calendar_event.integration_account else "microsoft")
        meeting_title = calendar_event.title if calendar_event else None
        organizer_email = calendar_event.organizer_email if calendar_event else None

        if not transcript.transcript_text:
            transcript.processing_status = SOURCE_STATUS_NO_ACTION_REQUIRED
            await db.commit()
            continue

        source_type_label = "microsoft_teams_transcript"
        final_status = await handle_extracted_tasks(
            source_row=transcript,
            source_type="transcript",
            provenance_source_type=source_type_label,
            source_title=meeting_title or f"Meeting Transcript #{transcript.id}",
            source_text=transcript.transcript_text,
            source_external_id=transcript.provider_transcript_id or str(transcript.id),
            provider=provider,
            source_date=ensure_aware_utc(calendar_event.starts_at) if calendar_event else None,
            fallback_assignee_email=organizer_email,
        )
        transcript.processing_status = final_status
        if final_status == SOURCE_STATUS_FAILED:
            sources_failed += 1
        await db.commit()

    if sync_run is not None:
        sync_run.analyzed_count += sources_analyzed
        sync_run.actionable_count += tasks_created + tasks_needing_review
        sync_run.tasks_created_count += tasks_created
        sync_run.skipped_count += emails_skipped_as_non_task + emails_without_text
        sync_run.failed_count += sources_failed

    logger.info(
        "ai task creation done",
        extra={
            "user_id": user.id, "organization_id": str(org_id),
            "emails": len(emails), "transcripts": len(transcripts),
            "sources_analyzed": sources_analyzed, "tasks_created": tasks_created,
            "needs_review": tasks_needing_review, "skipped_non_task": emails_skipped_as_non_task,
            "failed": sources_failed,
        },
    )

    return {
        "emails_found": len(emails),
        "transcripts_found": len(transcripts),
        "sources_analyzed": sources_analyzed,
        "tasks_created": tasks_created,
        "tasks_needing_review": tasks_needing_review,
        "tasks_created_without_assignee": tasks_created_without_assignee,
        "tasks_created_without_project": tasks_created_without_project,
        "tasks_created_without_team": tasks_created_without_team,
        "tasks_created_without_start_date": tasks_created_without_start_date,
        "tasks_created_without_due_date": tasks_created_without_due_date,
        "emails_skipped_as_non_task": emails_skipped_as_non_task,
        "emails_without_text": emails_without_text,
        "sources_failed": sources_failed,
    }
