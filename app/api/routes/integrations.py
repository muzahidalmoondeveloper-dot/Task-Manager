import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.activity_actions import ENTITY_INTEGRATION, INTEGRATION_CONNECTED, INTEGRATION_DISCONNECTED
from app.core.automation_pipeline import ERROR_UNKNOWN, SYNC_RUN_FAILED, SYNC_RUN_PARTIAL_SUCCESS
from app.core.config import settings
from app.core.database import get_db
from app.core.tenant import TenantContext, enforce_feature, get_tenant_context, require_org_admin
from app.models.user import User
from app.repositories.integration_repository import IntegrationRepository
from app.schemas.integration import IntegrationAccountRead
from app.services import activity_service
from app.models.integration import (
    CalendarEvent,
    ImportedEmail,
    IntegrationAccount,
    MeetingTranscript,
    SyncRun,
    TaskSource,
)
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
logger = logging.getLogger("integrations")


# Automation Pipeline Audit follow-up: this route module used to carry
# its own copies of parse_graph_datetime/parse_iso_datetime/
# extract_email_address/extract_recipients/get_yesterday_range_utc,
# duplicating (and, in the buggy sync_recent_microsoft_data route below,
# diverging from) the canonical versions in app.services.automation_tasks.
# All fetch/normalize logic now lives exclusively there — see
# _run_provider_sync below — so those duplicates were removed rather than
# kept in sync by hand.


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

    # Task #8B — user_id/org_id both come from the trusted, server-signed
    # `state` payload (never from the OAuth provider's own response), so
    # this is a safely-scoped, genuine connection event. Metadata is
    # strictly the provider identity — NEVER the access_token,
    # refresh_token, or scopes, all of which are present a few lines above
    # in this same function.
    actor = await db.get(User, state_data["user_id"])
    await activity_service.record(
        db, organization_id=org.id, actor=actor, action=INTEGRATION_CONNECTED,
        entity_type=ENTITY_INTEGRATION, entity_id=None, entity_label="Google Calendar",
        metadata={"provider": "google"},
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

    provider = account.provider
    account_email = account.account_email

    await db.execute(
        delete(IntegrationAccount).where(
            IntegrationAccount.id == account.id
        )
    )

    await db.commit()

    # Task #8B — provider/email captured above before the row is deleted,
    # since account.provider isn't safely readable after the delete.
    # Metadata is provider identity only — never any credential.
    await activity_service.record(
        db, organization_id=tenant.organization_id, actor=tenant.user,
        action=INTEGRATION_DISCONNECTED, entity_type=ENTITY_INTEGRATION, entity_id=None,
        entity_label=provider.capitalize() if provider else None,
        metadata={"provider": provider},
    )

    return {
        "message": f"{provider.capitalize()} account disconnected.",
        "provider": provider,
        "account_email": account_email,
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

    # Task #8B — mirrors google_callback()'s activity logging above: trusted
    # server-signed state, provider-identity-only metadata, never tokens/scopes.
    actor2 = await db.get(User, state_data["user_id"])
    await activity_service.record(
        db, organization_id=org2.id, actor=actor2, action=INTEGRATION_CONNECTED,
        entity_type=ENTITY_INTEGRATION, entity_id=None, entity_label="Microsoft 365",
        metadata={"provider": "microsoft"},
    )

    return RedirectResponse(f"{settings.FRONTEND_URL}/integrations?connected=microsoft")




def _summarize_sync_run(run) -> dict:
    return {
        "id": run.id,
        "provider": run.provider,
        "trigger": run.trigger,
        "status": run.status,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "fetched_count": run.fetched_count,
        "analyzed_count": run.analyzed_count,
        "actionable_count": run.actionable_count,
        "tasks_created_count": run.tasks_created_count,
        "duplicate_count": run.duplicate_count,
        "skipped_count": run.skipped_count,
        "failed_count": run.failed_count,
        "last_error_code": run.last_error_code,
        "last_error_message": run.last_error_message,
    }


async def _run_provider_sync(
    *,
    db: AsyncSession,
    tenant: TenantContext,
    provider: str,
    sync_fn,
) -> dict:
    """Shared body for the two manual "Sync Recent Data" routes below —
    the one thing Gmail and Microsoft manual sync actually have in
    common at the route layer: resolve accounts, open a Sync Run,
    FETCH -> ANALYZE, close the run with a real status derived from what
    happened, and return a rich, honest summary (never "success" just
    because the HTTP call returned 200 — see AUTOMATION_PIPELINE's own
    module docstring / the audit's "Do not equate Connected with
    Automation Working").

    Runs synchronously in the request (this app has no active queue —
    see automation_scheduler.py's own docstring: "no Celery/Redis
    required" — the existing architectural choice this follow-up reuses
    rather than replaces with a second queue system). Bounded by the
    same per-account page-size caps the adapters already use, so this
    stays fast enough for a manual button click; the SAME Sync Run row
    this call creates is exactly what a future async/queued version
    would also update, so moving to background execution later is an
    additive change, not a rewrite.
    """
    from app.services.automation_tasks import (
        analyze_pending_sources_for_user,
        create_sync_run,
        finish_sync_run,
    )

    enforce_feature(tenant, "has_integrations")
    repository = IntegrationRepository(db, tenant.organization_id)
    accounts = await repository.list_accounts_by_provider(tenant.user.id, provider)
    if not accounts:
        raise HTTPException(status_code=400, detail=f"No {provider.capitalize()} account connected for this user.")

    sync_run = await create_sync_run(
        db, organization_id=tenant.organization_id, provider=provider,
        integration_account_id=accounts[0].id if len(accounts) == 1 else None,
        triggered_by_user_id=tenant.user.id, trigger="manual",
    )

    fatal_error_code = None
    sync_result: dict = {}
    ai_result: dict = {}
    try:
        sync_result = await sync_fn(db=db, user=tenant.user, sync_run=sync_run)
        ai_result = await analyze_pending_sources_for_user(
            db=db, user=tenant.user, org_id=tenant.organization_id, sync_run=sync_run,
        )
    except Exception:
        logger.exception(
            "manual sync failed",
            extra={"provider": provider, "sync_run_id": sync_run.id, "organization_id": str(tenant.organization_id), "user_id": tenant.user.id},
        )
        fatal_error_code = ERROR_UNKNOWN

    sync_run = await finish_sync_run(db, sync_run, fatal_error_code=fatal_error_code)

    fetched = sync_result.get("emails_imported", 0) + sync_result.get("calendar_events_imported", 0) + sync_result.get("transcripts_imported", 0)
    tasks_created = ai_result.get("tasks_created", 0)
    needs_review = ai_result.get("tasks_needing_review", 0)
    analyzed = ai_result.get("sources_analyzed", 0)

    if sync_run.status == SYNC_RUN_FAILED:
        message = f"Sync failed: {sync_run.last_error_message or 'an unexpected error occurred.'}"
    elif sync_run.status == SYNC_RUN_PARTIAL_SUCCESS:
        message = (
            f"Sync partially completed. {fetched} items fetched, {analyzed} analyzed, "
            f"{tasks_created} task(s) created, {needs_review} sent for review — some items failed."
        )
    else:
        message = (
            f"Sync complete. {fetched} items fetched, {analyzed} analyzed, "
            f"{tasks_created} task(s) created"
            + (f", {needs_review} sent for review" if needs_review else "")
            + "."
        )

    return {
        "sync_run": _summarize_sync_run(sync_run),
        "fetch": sync_result,
        "analysis": ai_result,
        "message": message,
    }


@router.post("/microsoft/sync-recent")
async def sync_recent_microsoft_data(
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db),
):
    """Manual "Sync Recent Microsoft Data" — Microsoft-sync `current_user`
    NameError fix (Automation Pipeline Audit): this route previously
    referenced an undefined `current_user` (should have been the
    canonical authenticated user already available as `tenant.user` from
    `TenantContext`) and duplicated ~300 lines of the same fetch logic
    `app.services.automation_tasks.sync_microsoft_data_for_user` already
    implements correctly — with its own debug `print()` statements and,
    critically, NO call into AI analysis/Task creation at all. A
    "successful" sync here never actually created a Task; it only ever
    imported raw emails/events/transcripts.

    Fixed by deleting the duplicate implementation entirely and routing
    through the same canonical FETCH -> ANALYZE pipeline the scheduler
    uses (see _run_provider_sync above) — the authenticated user is
    always `tenant.user`, never a free-standing name that was never
    defined."""
    from app.services.automation_tasks import sync_microsoft_data_for_user

    logger.info(
        "microsoft manual sync requested",
        extra={"organization_id": str(tenant.organization_id), "user_id": tenant.user.id},
    )
    return await _run_provider_sync(db=db, tenant=tenant, provider="microsoft", sync_fn=sync_microsoft_data_for_user)


@router.post("/google/sync-recent")
async def sync_recent_google_data(
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db),
):
    """Manual "Sync Recent Gmail Data" (Automation Pipeline Audit,
    Phase 2) — there was previously no Gmail fetch/analysis path at all,
    only OAuth connect/callback (tokens got stored and nothing ever read
    them). This is the Gmail counterpart to the Microsoft route above,
    sharing the identical FETCH -> ANALYZE pipeline."""
    from app.services.automation_tasks import sync_gmail_data_for_user

    logger.info(
        "gmail manual sync requested",
        extra={"organization_id": str(tenant.organization_id), "user_id": tenant.user.id},
    )
    return await _run_provider_sync(db=db, tenant=tenant, provider="google", sync_fn=sync_gmail_data_for_user)


@router.get("/status")
async def get_integration_status(
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Automation Pipeline Audit — "Connected" is not "Automation
    Working" (see the audit's own success-criteria list). Backs the
    upgraded Integration cards: per-connected-account last-sync
    checkpoint/status/error plus its most recent Sync Run's full
    counters, so a user can answer "when did it last successfully sync"
    and "what actually happened" without reading server logs.
    """
    enforce_feature(tenant, "has_integrations")
    repository = IntegrationRepository(tenant.db, tenant.organization_id)
    accounts = await repository.list_accounts(tenant.user.id)

    result = await tenant.db.execute(
        select(SyncRun)
        .where(SyncRun.organization_id == tenant.organization_id, SyncRun.triggered_by_user_id == tenant.user.id)
        .order_by(SyncRun.started_at.desc())
    )
    all_runs = list(result.scalars().all())
    latest_run_by_provider: dict[str, SyncRun] = {}
    for run in all_runs:
        latest_run_by_provider.setdefault(run.provider, run)

    accounts_out = []
    for account in accounts:
        latest_run = latest_run_by_provider.get(account.provider)
        accounts_out.append({
            "id": account.id,
            "provider": account.provider,
            "account_email": account.account_email,
            "last_synced_at": account.last_synced_at.isoformat() if account.last_synced_at else None,
            "last_sync_status": account.last_sync_status,
            "last_sync_error": account.last_sync_error,
            "latest_run": _summarize_sync_run(latest_run) if latest_run else None,
        })

    return {"accounts": accounts_out}


@router.get("/sync-runs")
async def list_sync_runs(
    provider: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Automation Activity feed (paginated — never the entire history at
    once, per the audit's UX requirement)."""
    enforce_feature(tenant, "has_integrations")
    stmt = (
        select(SyncRun)
        .where(SyncRun.organization_id == tenant.organization_id, SyncRun.triggered_by_user_id == tenant.user.id)
        .order_by(SyncRun.started_at.desc())
        .offset(offset)
        .limit(limit)
    )
    if provider:
        stmt = stmt.where(SyncRun.provider == provider)
    result = await tenant.db.execute(stmt)
    runs = list(result.scalars().all())
    return {"runs": [_summarize_sync_run(run) for run in runs], "limit": limit, "offset": offset}


@router.get("/activity-items")
async def list_activity_items(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Per-item Automation Activity (spec: Time | Source | Item |
    Processing Status | AI Result | Task Result) — one row per email/
    transcript this user's connected accounts have ever imported, newest
    first, paginated. Deliberately returns only safe display metadata
    (subject/title/date/status/short AI reason) — never body/transcript
    text — and the count of Tasks actually created from that item via
    TaskSource, not a guess."""
    account_ids_result = await tenant.db.execute(
        select(IntegrationAccount.id).where(
            IntegrationAccount.user_id == tenant.user.id,
            IntegrationAccount.organization_id == tenant.organization_id,
        )
    )
    account_ids = [row[0] for row in account_ids_result.all()]
    if not account_ids:
        return {"items": [], "limit": limit, "offset": offset}

    email_stmt = (
        select(ImportedEmail)
        .where(ImportedEmail.integration_account_id.in_(account_ids))
        .order_by(ImportedEmail.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    transcript_stmt = (
        select(MeetingTranscript)
        .join(CalendarEvent, MeetingTranscript.calendar_event_id == CalendarEvent.id)
        .where(CalendarEvent.integration_account_id.in_(account_ids))
        .order_by(MeetingTranscript.created_at.desc())
        .limit(limit)
        .offset(offset)
        .options(selectinload(MeetingTranscript.calendar_event))
    )
    emails = list((await tenant.db.execute(email_stmt)).scalars().all())
    transcripts = list((await tenant.db.execute(transcript_stmt)).scalars().all())

    task_counts: dict[tuple[str, str], int] = {}
    if emails or transcripts:
        source_keys = [("email", e.provider_message_id) for e in emails] + [
            ("transcript", t.provider_transcript_id or str(t.id)) for t in transcripts
        ]
        result = await tenant.db.execute(
            select(TaskSource.source_type, TaskSource.source_external_id)
            .where(
                TaskSource.organization_id == tenant.organization_id,
                TaskSource.source_external_id.in_([k[1] for k in source_keys]),
            )
        )
        for source_type, source_external_id in result.all():
            key = ("email" if source_type != "microsoft_teams_transcript" else "transcript", source_external_id)
            task_counts[key] = task_counts.get(key, 0) + 1

    items = []
    for email in emails:
        items.append({
            "time": email.created_at.isoformat() if email.created_at else None,
            "source": "gmail" if (email.integration_account and email.integration_account.provider == "google") else "outlook",
            "item_title": email.subject,
            "processing_status": email.processing_status,
            "ai_result_summary": email.ai_result_summary,
            "tasks_created": task_counts.get(("email", email.provider_message_id), 0),
            "error": email.last_error,
        })
    for transcript in transcripts:
        items.append({
            "time": transcript.created_at.isoformat() if transcript.created_at else None,
            "source": "teams",
            "item_title": transcript.calendar_event.title if transcript.calendar_event else f"Meeting Transcript #{transcript.id}",
            "processing_status": transcript.processing_status,
            "ai_result_summary": transcript.ai_result_summary,
            "tasks_created": task_counts.get(("transcript", transcript.provider_transcript_id or str(transcript.id)), 0),
            "error": transcript.last_error,
        })

    items.sort(key=lambda i: i["time"] or "", reverse=True)
    return {"items": items[:limit], "limit": limit, "offset": offset}
