import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.auth_errors import AppException
from app.models.organization import OrganizationMembership
from app.models.integration import (
    CalendarEvent,
    ImportedEmail,
    IntegrationAccount,
    MeetingTranscript,
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
    text = f"{subject or ''} {body or ''} {sender or ''}".lower()
    return any(keyword in text for keyword in SPAM_OR_AD_KEYWORDS)


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


async def sync_microsoft_data_for_user(
    *,
    db: AsyncSession,
    user: User,
) -> dict:
    org_id = await resolve_org_id_for_user(db, user)
    if org_id is None:
        return {
            "emails_imported": 0,
            "calendar_events_imported": 0,
            "transcripts_imported": 0,
            "transcript_errors": [],
        }

    repository = IntegrationRepository(db, org_id)

    accounts = await repository.list_accounts_by_provider(
        user.id,
        "microsoft",
    )

    if not accounts:
        return {
            "emails_imported": 0,
            "calendar_events_imported": 0,
            "transcripts_imported": 0,
            "transcript_errors": [],
        }

    start_at, end_at = get_yesterday_range_utc()
    start_at = ensure_aware_utc(start_at)
    end_at = ensure_aware_utc(end_at)

    total_emails = 0
    total_events = 0
    total_transcripts = 0
    transcript_errors = []

    logger.info(
        "Microsoft sync range for %s: %s -> %s",
        user.email,
        start_at.isoformat(),
        end_at.isoformat(),
    )

    for account in accounts:
        graph = MicrosoftGraphService(account)
        await graph.ensure_fresh_token(db)

        logger.info("Syncing Microsoft account: %s", account.account_email)

        try:
            messages = await graph.list_messages(start_at, end_at)
        except Exception as exc:
            logger.exception(
                "Microsoft email sync failed for %s",
                account.account_email,
            )
            messages = []

        for message in messages:
            received_at = parse_iso_datetime(message.get("receivedDateTime"))

            if not is_datetime_in_range(received_at, start_at, end_at):
                logger.info(
                    "Skipping email outside yesterday range. subject=%s received_at=%s",
                    message.get("subject"),
                    received_at,
                )
                continue

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
            logger.exception(
                "Microsoft calendar sync failed for %s",
                account.account_email,
            )
            events = []

        for event in events:
            event_starts_at = parse_graph_datetime(event.get("start"))
            event_ends_at = parse_graph_datetime(event.get("end"))

            if not is_datetime_in_range(event_starts_at, start_at, end_at):
                logger.info(
                    "Skipping calendar event outside yesterday range. title=%s starts_at=%s",
                    event.get("subject"),
                    event_starts_at,
                )
                continue

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
                    found_meeting = await graph.find_online_meeting_by_join_url(
                        meeting_url
                    )

                    if found_meeting:
                        online_meeting_id = found_meeting.get("id")

                except Exception as exc:
                    transcript_errors.append(
                        {
                            "event_id": event.get("id"),
                            "title": event.get("subject"),
                            "stage": "resolve_online_meeting",
                            "error": str(exc),
                        }
                    )

            if not online_meeting_id:
                logger.info(
                    "Skipping transcript fetch. No online meeting id. event=%s",
                    event.get("subject"),
                )
                continue

            try:
                transcripts = await graph.list_transcripts_for_online_meeting(
                    online_meeting_id
                )

                for transcript in transcripts:
                    transcript_timestamp = parse_transcript_datetime(transcript)

                    if transcript_timestamp and not is_datetime_in_range(
                        transcript_timestamp,
                        start_at,
                        end_at,
                    ):
                        logger.info(
                            "Skipping old transcript. event=%s transcript_id=%s transcript_time=%s",
                            event.get("subject"),
                            transcript.get("id"),
                            transcript_timestamp,
                        )
                        continue

                    if not transcript_timestamp and not is_datetime_in_range(
                        event_starts_at,
                        start_at,
                        end_at,
                    ):
                        logger.info(
                            "Skipping transcript without timestamp because event is outside range. event=%s transcript_id=%s",
                            event.get("subject"),
                            transcript.get("id"),
                        )
                        continue

                    transcript_text = await graph.get_transcript_content(
                        online_meeting_id,
                        transcript["id"],
                    )

                    if not transcript_text:
                        logger.info(
                            "Skipping empty transcript. event=%s transcript_id=%s",
                            event.get("subject"),
                            transcript.get("id"),
                        )
                        continue

                    await repository.upsert_meeting_transcript(
                        calendar_event_id=saved_event.id,
                        provider_transcript_id=transcript["id"],
                        transcript_text=transcript_text,
                        raw_payload={
                            **transcript,
                            "event_starts_at": event_starts_at.isoformat()
                            if event_starts_at
                            else None,
                            "event_ends_at": event_ends_at.isoformat()
                            if event_ends_at
                            else None,
                        },
                    )

                    total_transcripts += 1

            except Exception as exc:
                transcript_errors.append(
                    {
                        "event_id": event.get("id"),
                        "title": event.get("subject"),
                        "stage": "fetch_transcripts",
                        "error": str(exc),
                    }
                )

    logger.info(
        "Microsoft sync done for %s. emails=%s events=%s transcripts=%s",
        user.email,
        total_emails,
        total_events,
        total_transcripts,
    )

    return {
        "emails_imported": total_emails,
        "calendar_events_imported": total_events,
        "transcripts_imported": total_transcripts,
        "transcript_errors": transcript_errors,
    }


async def _mark_email_extracted(db: AsyncSession, email_id: int) -> None:
    await db.execute(
        sql_update(ImportedEmail)
        .where(ImportedEmail.id == email_id)
        .values(tasks_extracted=True)
    )
    await db.commit()


async def _mark_transcript_extracted(db: AsyncSession, transcript_id: int) -> None:
    await db.execute(
        sql_update(MeetingTranscript)
        .where(MeetingTranscript.id == transcript_id)
        .values(tasks_extracted=True)
    )
    await db.commit()


async def analyze_yesterday_sources_for_user(
    *,
    db: AsyncSession,
    user: User,
    org_id,
) -> dict:
    extractor = AITaskExtractor()
    task_repo = TaskRepository(db, org_id)
    user_repo = UserRepository(db)
    project_repo = ProjectRepository(db, org_id)
    team_repo = TeamRepository(db, org_id)

    # SECURITY: org-scoped candidate set only (cross-tenant automation-
    # assignee fix) — NEVER user_repo.list_all(), which scans every user in
    # the entire database with no organization boundary. Active, non-Client
    # members of THIS organization only; carries each user's authoritative
    # OrganizationMembership.role (never the legacy/global User.role) for
    # the Admin-fallback check below.
    user_candidates = await user_repo.list_org_assignable_candidates(org_id)
    projects = await project_repo.list_all()
    teams = await team_repo.list_all()

    email_statement = (
        select(ImportedEmail)
        .join(
            IntegrationAccount,
            ImportedEmail.integration_account_id == IntegrationAccount.id,
        )
        .where(IntegrationAccount.user_id == user.id)
        .where(ImportedEmail.tasks_extracted.is_(False))
        .order_by(ImportedEmail.received_at.desc())
    )

    email_result = await db.execute(email_statement)
    emails = list(email_result.scalars().all())

    transcript_statement = (
        select(MeetingTranscript)
        .join(
            CalendarEvent,
            MeetingTranscript.calendar_event_id == CalendarEvent.id,
        )
        .join(
            IntegrationAccount,
            CalendarEvent.integration_account_id == IntegrationAccount.id,
        )
        .where(IntegrationAccount.user_id == user.id)
        .where(MeetingTranscript.tasks_extracted.is_(False))
        .options(selectinload(MeetingTranscript.calendar_event))
        .order_by(CalendarEvent.starts_at.desc())
    )

    transcript_result = await db.execute(transcript_statement)
    transcripts = list(transcript_result.scalars().all())

    sources_analyzed = 0
    tasks_created = 0
    tasks_created_without_assignee = 0
    tasks_created_without_project = 0
    tasks_created_without_team = 0
    tasks_created_without_start_date = 0
    tasks_created_without_due_date = 0
    emails_skipped_as_non_task = 0
    emails_without_text = 0

    async def handle_extracted_tasks(
        *,
        source_type: str,
        source_title: str | None,
        source_text: str,
        fallback_assignee_email: str | None = None,
    ):
        nonlocal sources_analyzed
        nonlocal tasks_created
        nonlocal tasks_created_without_assignee
        nonlocal tasks_created_without_project
        nonlocal tasks_created_without_team
        nonlocal tasks_created_without_start_date
        nonlocal tasks_created_without_due_date

        # Privacy (PHASE 21) + tenant safety: the AI extractor's
        # "known users" hint list is built from the org-scoped candidate
        # set only — never leaks another organization's user names/emails
        # into this org's extraction prompt.
        known_users_list = [
            {"name": u.full_name, "email": u.email}
            for u, _role in user_candidates
        ]

        extracted_tasks, raw_payload = await extractor.extract_tasks(
            source_type=source_type,
            source_title=source_title,
            source_text=source_text,
            known_users=known_users_list,
        )

        sources_analyzed += 1

        for extracted_task in extracted_tasks:
            # Team resolution happens BEFORE assignee resolution (PHASE 13/
            # 14) — a Team Task's assignee boundary is that exact Team, so
            # the candidate set for both explicit name/email matching and
            # fallback selection must already be narrowed to the team
            # before either runs, never widened back to the whole org.
            team_id = resolve_team_id(
                teams,
                extracted_task.suggested_team_name,
            )

            if team_id is None and source_type == "transcript":
                team_id = resolve_team_from_meeting_title(teams, source_title)

            if team_id is not None:
                team_members = await team_repo.list_assignable_members(team_id)
                assignee_pool = team_members
                eligible_ids = {u.id for u in team_members}
            else:
                assignee_pool = [u for u, _role in user_candidates]
                eligible_ids = None

            assignee_id = resolve_user_id(
                assignee_pool,
                extracted_task.suggested_assignee_name,
                extracted_task.suggested_assignee_email,
            )

            if assignee_id is None:
                assignee_id = find_fallback_assignee_id(
                    user_candidates,
                    fallback_assignee_email,
                    user.id,
                    eligible_ids=eligible_ids,
                )
                tasks_created_without_assignee += 1

            project_id = resolve_project_id(
                projects,
                extracted_task.suggested_project_name,
            )

            if project_id is None:
                tasks_created_without_project += 1

            if team_id is None:
                tasks_created_without_team += 1

            if extracted_task.suggested_start_date is None:
                tasks_created_without_start_date += 1

            if extracted_task.suggested_due_date is None:
                tasks_created_without_due_date += 1

            try:
                await task_repo.create(
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
                    "Automation-resolved assignee %s failed final validation for org %s — creating task unassigned instead.",
                    assignee_id, org_id,
                )
                await task_repo.create(
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
                tasks_created_without_assignee += 1

            tasks_created += 1

    for email in emails:
        source_text = email.body_text or email.snippet

        if not source_text:
            emails_without_text += 1
            await _mark_email_extracted(db, email.id)
            continue

        if is_probably_non_task_email(email.subject, source_text, email.sender):
            emails_skipped_as_non_task += 1
            await _mark_email_extracted(db, email.id)
            continue

        await handle_extracted_tasks(
            source_type="email",
            source_title=email.subject,
            source_text=source_text,
            fallback_assignee_email=email.sender,
        )

        await _mark_email_extracted(db, email.id)

    for transcript in transcripts:
        if not transcript.transcript_text:
            await _mark_transcript_extracted(db, transcript.id)
            continue

        meeting_title = None
        organizer_email = None

        if transcript.calendar_event:
            meeting_title = transcript.calendar_event.title
            organizer_email = transcript.calendar_event.organizer_email

        await handle_extracted_tasks(
            source_type="transcript",
            source_title=meeting_title or f"Meeting Transcript #{transcript.id}",
            source_text=transcript.transcript_text,
            fallback_assignee_email=organizer_email,
        )

        await _mark_transcript_extracted(db, transcript.id)

    logger.info(
            "AI task creation done for %s. emails=%s transcripts=%s sources=%s tasks=%s skipped_email=%s",
            user.email,
            len(emails),
            len(transcripts),
            sources_analyzed,
            tasks_created,
            emails_skipped_as_non_task,
        )

    return {
        "emails_found": len(emails),
        "transcripts_found": len(transcripts),
        "sources_analyzed": sources_analyzed,
        "tasks_created": tasks_created,
        "tasks_created_without_assignee": tasks_created_without_assignee,
        "tasks_created_without_project": tasks_created_without_project,
        "tasks_created_without_team": tasks_created_without_team,
        "tasks_created_without_start_date": tasks_created_without_start_date,
        "tasks_created_without_due_date": tasks_created_without_due_date,
        "emails_skipped_as_non_task": emails_skipped_as_non_task,
        "emails_without_text": emails_without_text,
    }