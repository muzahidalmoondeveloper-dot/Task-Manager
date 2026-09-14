"""Gmail + Microsoft AI automation pipeline — shared, provider-neutral
constants and error classification (Automation Pipeline Audit follow-up).

Every provider adapter (Microsoft Graph, Gmail) fetches and normalizes
provider-specific data into the SAME rows (`ImportedEmail`,
`MeetingTranscript`) using the SAME `IntegrationRepository` upsert calls.
Everything downstream of that point — idempotent claiming, AI analysis,
confidence gating, Task creation, provenance, Sync Run bookkeeping — is
one shared implementation in `app.services.automation_tasks`, never a
per-provider copy. This module holds the vocabulary that pipeline uses so
routes, the scheduler, and the two provider adapters all speak the same
status/error language instead of inventing ad-hoc strings.
"""

from __future__ import annotations

import httpx

# ── Sync Run status (app.models.integration.SyncRun.status) ────────────────
SYNC_RUN_QUEUED = "queued"
SYNC_RUN_RUNNING = "running"
SYNC_RUN_SUCCESS = "success"
SYNC_RUN_PARTIAL_SUCCESS = "partial_success"
SYNC_RUN_FAILED = "failed"

SYNC_RUN_STATUSES = frozenset({
    SYNC_RUN_QUEUED, SYNC_RUN_RUNNING, SYNC_RUN_SUCCESS,
    SYNC_RUN_PARTIAL_SUCCESS, SYNC_RUN_FAILED,
})

SYNC_TRIGGER_MANUAL = "manual"
SYNC_TRIGGER_SCHEDULED = "scheduled"
SYNC_TRIGGER_RETRY = "retry"

# ── Source-item processing status (ImportedEmail/MeetingTranscript.
# processing_status) — deliberately a smaller set than the spec's full
# suggested lifecycle: "discovered" and "fetched" collapse into one state
# here because the existing adapters only ever persist a row once they
# already have its full content (there is no separate discovery step that
# stores an item before content is available — see the module docstring
# in automation_tasks.py for why waiting_for_transcript is tracked on
# CalendarEvent instead, since a meeting can be "discovered" long before
# any transcript row exists at all). ──────────────────────────────────────
SOURCE_STATUS_DISCOVERED = "discovered"       # imported, not yet analyzed
SOURCE_STATUS_ANALYZING = "analyzing"         # claimed by a sync run, in progress
SOURCE_STATUS_NO_ACTION_REQUIRED = "no_action_required"
SOURCE_STATUS_NEEDS_REVIEW = "needs_review"   # actionable but below the auto-create confidence bar
SOURCE_STATUS_TASK_CREATED = "task_created"
SOURCE_STATUS_PARTIALLY_CREATED = "partially_created"  # multiple action items, only some became Tasks
SOURCE_STATUS_FAILED = "failed"

SOURCE_ITEM_STATUSES = frozenset({
    SOURCE_STATUS_DISCOVERED, SOURCE_STATUS_ANALYZING, SOURCE_STATUS_NO_ACTION_REQUIRED,
    SOURCE_STATUS_NEEDS_REVIEW, SOURCE_STATUS_TASK_CREATED, SOURCE_STATUS_PARTIALLY_CREATED,
    SOURCE_STATUS_FAILED,
})

# ── Meeting transcript availability (CalendarEvent.transcript_status) ──────
TRANSCRIPT_STATUS_UNKNOWN = "unknown"            # not checked yet
TRANSCRIPT_STATUS_WAITING = "waiting"            # checked, not ready — will retry
TRANSCRIPT_STATUS_AVAILABLE = "available"        # fetched successfully
TRANSCRIPT_STATUS_PERMISSION_DENIED = "permission_denied"  # permanent — Graph rejected access
TRANSCRIPT_STATUS_EXPIRED = "expired"            # gave up after the retry budget
TRANSCRIPT_STATUS_NOT_SUPPORTED = "not_supported"  # meeting has no online-meeting id at all

# Capped-backoff schedule for re-checking a meeting whose transcript wasn't
# ready yet (minutes). Deliberately bounded, not an infinite poll — after
# this many attempts the event is marked TRANSCRIPT_STATUS_EXPIRED rather
# than checked forever (see the spec's own "do not create infinite polling
# loops" constraint).
TRANSCRIPT_RETRY_BACKOFF_MINUTES = [5, 15, 45, 120, 240]
TRANSCRIPT_MAX_ATTEMPTS = len(TRANSCRIPT_RETRY_BACKOFF_MINUTES)


def transcript_backoff_minutes(attempt_number: int) -> int:
    """attempt_number is 1-indexed (the attempt that just ran)."""
    index = min(attempt_number, len(TRANSCRIPT_RETRY_BACKOFF_MINUTES)) - 1
    return TRANSCRIPT_RETRY_BACKOFF_MINUTES[max(index, 0)]


# ── Error classification (spec's "ERROR CLASSIFICATION" section) ───────────
# A safe, user-facing code + message pair — never a raw stack trace, never
# a token/secret. Used for SyncRun.last_error_code/message and for each
# source item's own last_error, and by the Integration status endpoint's
# "Last error" line.
ERROR_AUTH_EXPIRED = "AUTH_EXPIRED"
ERROR_TOKEN_REFRESH_FAILED = "TOKEN_REFRESH_FAILED"
ERROR_PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"
ERROR_PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
ERROR_TRANSCRIPT_NOT_READY = "TRANSCRIPT_NOT_READY"
ERROR_TRANSCRIPT_PERMISSION_DENIED = "TRANSCRIPT_PERMISSION_DENIED"
ERROR_AI_ANALYSIS_FAILED = "AI_ANALYSIS_FAILED"
ERROR_AI_OUTPUT_INVALID = "AI_OUTPUT_INVALID"
ERROR_TASK_VALIDATION_FAILED = "TASK_VALIDATION_FAILED"
ERROR_TASK_CREATION_FAILED = "TASK_CREATION_FAILED"
ERROR_DUPLICATE_SKIPPED = "DUPLICATE_SKIPPED"
ERROR_UNKNOWN = "UNKNOWN"

_SAFE_ERROR_MESSAGES = {
    ERROR_AUTH_EXPIRED: "The connected account's authorization has expired. Please reconnect it.",
    ERROR_TOKEN_REFRESH_FAILED: "We couldn't refresh this account's access token. Please reconnect it.",
    ERROR_PROVIDER_RATE_LIMITED: "The provider is temporarily rate-limiting requests. This will retry automatically.",
    ERROR_PROVIDER_UNAVAILABLE: "The provider was temporarily unavailable. This will retry automatically.",
    ERROR_TRANSCRIPT_NOT_READY: "The meeting transcript is not available yet.",
    ERROR_TRANSCRIPT_PERMISSION_DENIED: "This account doesn't have permission to read Teams transcripts.",
    ERROR_AI_ANALYSIS_FAILED: "AI analysis could not complete for this item.",
    ERROR_AI_OUTPUT_INVALID: "AI analysis returned an unexpected result and was skipped for safety.",
    ERROR_TASK_VALIDATION_FAILED: "The suggested task could not be validated.",
    ERROR_TASK_CREATION_FAILED: "The task could not be created.",
    ERROR_DUPLICATE_SKIPPED: "This item was already processed and was skipped.",
    ERROR_UNKNOWN: "Something went wrong while processing this item.",
}

# Transient — safe to retry with capped exponential backoff.
TRANSIENT_ERROR_CODES = frozenset({
    ERROR_PROVIDER_RATE_LIMITED, ERROR_PROVIDER_UNAVAILABLE,
    ERROR_TRANSCRIPT_NOT_READY, ERROR_AI_ANALYSIS_FAILED,
})

# Permanent — never auto-retried; surfaced to the user as action-required.
PERMANENT_ERROR_CODES = frozenset({
    ERROR_AUTH_EXPIRED, ERROR_TOKEN_REFRESH_FAILED,
    ERROR_TRANSCRIPT_PERMISSION_DENIED, ERROR_AI_OUTPUT_INVALID,
    ERROR_TASK_VALIDATION_FAILED,
})


def safe_error_message(code: str) -> str:
    return _SAFE_ERROR_MESSAGES.get(code, _SAFE_ERROR_MESSAGES[ERROR_UNKNOWN])


def classify_http_error(exc: Exception) -> str:
    """Maps a provider HTTP failure to one of the codes above. Never
    inspects/logs the response body (may contain account-identifying
    data) — status code only."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 401:
            return ERROR_AUTH_EXPIRED
        if status == 403:
            return ERROR_TRANSCRIPT_PERMISSION_DENIED
        if status == 429:
            return ERROR_PROVIDER_RATE_LIMITED
        if status >= 500:
            return ERROR_PROVIDER_UNAVAILABLE
        return ERROR_UNKNOWN
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return ERROR_PROVIDER_UNAVAILABLE
    return ERROR_UNKNOWN


# ── Confidence -> auto-create gate (spec's "CONFIDENCE + HUMAN REVIEW") ────
# HIGH confidence auto-creates a Task; MEDIUM/ambiguous is recorded as
# needs_review (visible in Automation Activity) without creating a Task;
# LOW/no actionable work never creates a Task. Kept as a single named
# setting (not a bare literal scattered through automation_tasks.py) so it
# can be promoted to a real per-organization setting later without
# touching the pipeline logic — there is no existing production usage
# data to calibrate a numeric threshold against yet, so this starts at
# the spec's own recommended default rather than an invented number.
AUTO_CREATE_MIN_CONFIDENCE = "high"
_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def meets_auto_create_bar(confidence: str) -> bool:
    return _CONFIDENCE_RANK.get(confidence, 0) >= _CONFIDENCE_RANK.get(AUTO_CREATE_MIN_CONFIDENCE, 2)
