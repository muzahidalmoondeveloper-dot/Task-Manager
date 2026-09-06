"""Central write path for ActivityLog (Task #8) — the only place that
constructs an `ActivityLog` row, so every call site gets the same
identity/shape/sanitization guarantees instead of each route reinventing
them (and inevitably drifting on what's safe to store).

Transactional note (Phase 7): this app's existing repositories
(TaskRepository.create/update, TaskTimeEntryRepository.start/stop, ...)
already call `db.commit()` internally before a route can call this
service — there is no single open transaction spanning "business mutation
+ activity record" to join for most flows in this codebase's established
architecture, and restructuring those repositories to expose one would be
an unrelated, much larger refactor. So `record()` is called by each route
only *after* its business mutation has already been confirmed committed,
and performs its own commit for the log row. This guarantees an activity
record is only ever attempted for an actually-successful mutation (never
the reverse — a logged action whose business change didn't happen), at
the cost of a vanishingly small window where the mutation succeeds but
the log write itself then fails. That failure is never silently
swallowed — it's logged at ERROR level so it's visible to operators —
but it also never fails the user's already-successful request; the
alternative (raising 500 back to a user whose task really was created)
would be worse than a rare missed audit row.
"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.activity_actions import ALL_ACTIONS
from app.models.activity_log import ActivityLog
from app.models.user import User

logger = logging.getLogger("app.activity")

_MAX_LABEL_LENGTH = 255
_MAX_METADATA_KEYS = 10
_MAX_METADATA_VALUE_LENGTH = 300
_MAX_METADATA_LIST_LENGTH = 10

# Defense in depth: even though callers should never pass these, a
# metadata key matching (case-insensitively) any of these substrings is
# dropped rather than persisted. This is a backstop, not the only control —
# callers still must not construct metadata from raw request bodies.
_FORBIDDEN_METADATA_KEY_SUBSTRINGS = (
    "password", "hashed_password", "token", "secret", "api_key", "apikey",
    "credential", "authorization", "otp", "refresh_token", "access_token",
)


def _sanitize_metadata(metadata: dict | None) -> dict | None:
    if not metadata:
        return None
    clean: dict = {}
    for key, value in metadata.items():
        if len(clean) >= _MAX_METADATA_KEYS:
            break
        key_str = str(key)
        if any(bad in key_str.lower() for bad in _FORBIDDEN_METADATA_KEY_SUBSTRINGS):
            continue
        if isinstance(value, (dict, set, tuple, bytes)):
            # Only small, flat, JSON-primitive values (or a short list of
            # them, e.g. "fields_changed": [...]) are allowed — this is
            # audit context ("status: todo -> done"), never a nested object
            # graph or the mutated entity itself.
            continue
        if isinstance(value, list):
            if any(isinstance(item, (dict, list, set, tuple, bytes)) for item in value):
                continue
            clean[key_str] = [
                (str(item)[:_MAX_METADATA_VALUE_LENGTH] if isinstance(item, str) else item)
                for item in value[:_MAX_METADATA_LIST_LENGTH]
            ]
        elif value is None or isinstance(value, (bool, int, float)):
            clean[key_str] = value
        else:
            text = str(value)
            clean[key_str] = text[:_MAX_METADATA_VALUE_LENGTH]
    return clean or None


_FALLBACK_ACTOR_LABEL = "Unknown User"


def _actor_display_name(actor: User) -> str:
    """The immutable, long-lived `actor_label` snapshot must never contain
    an email address — ActivityLog is append-only and outlives the actor's
    account, so anything stored here is effectively permanent. Unlike the
    *live*-actor display convention used elsewhere in this app
    (app/api/routes/kpi.py, app/api/routes/notes.py: `full_name or email`),
    this snapshot uses `full_name` only, falling back to the same generic
    "Unknown User" label this module's own caller (activity_logs.py's
    `_serialize`) already uses for a nameless live actor — never the
    email, its local-part, phone, username-as-email, OAuth identity, or
    any other login/contact identifier. Deliberately excludes those plus
    role/tokens/profile-picture binary/any other sensitive field — see
    module docstring and ActivityLog.actor_label's own docstring for why."""
    name = (actor.full_name or "").strip()
    return name or _FALLBACK_ACTOR_LABEL


async def record(
    db: AsyncSession,
    *,
    organization_id,
    actor: User | None,
    action: str,
    entity_type: str | None = None,
    entity_id: int | None = None,
    entity_label: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Persists one ActivityLog row. `organization_id`/`actor` must always
    come from the server-resolved `TenantContext` (never from request
    payload fields, matching every other tenant-scoped write in this app)
    so `actor_label` can never be spoofed by a client (e.g. claiming
    actor_label="Admin") — it is always derived here, server-side, from the
    real authenticated actor, never accepted as a caller-supplied string.

    `actor_label` is an immutable snapshot of the actor's display name AT
    THIS MOMENT (Task #8 follow-up — see ActivityLog.actor_label's
    docstring): it is written once, here, and this module is the only
    place that ever writes it. It is never rewritten later if the user
    renames themselves, and it survives the user being deleted (at which
    point `actor_user_id` alone goes NULL via ON DELETE SET NULL — see
    ActivityLog's class docstring for why that's SET NULL, never CASCADE).

    Never raises: a failure here is logged and swallowed rather than
    turning a successful business mutation into a 500 for the user (see
    module docstring)."""
    if action not in ALL_ACTIONS:
        # Programmer error (an unregistered action string), not a runtime
        # condition — fail loudly in that case rather than silently
        # persisting an unrecognized value into a supposedly-closed
        # taxonomy.
        raise ValueError(f"Unknown activity action: {action!r} — add it to app.core.activity_actions first.")

    entry = ActivityLog(
        organization_id=organization_id,
        actor_user_id=actor.id if actor is not None else None,
        actor_label=_actor_display_name(actor) if actor is not None else None,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_label=(entity_label or "")[:_MAX_LABEL_LENGTH] or None,
        activity_metadata=_sanitize_metadata(metadata),
    )
    try:
        db.add(entry)
        await db.commit()
    except Exception:
        logger.exception("Failed to persist activity log entry: action=%s org=%s entity=%s/%s", action, organization_id, entity_type, entity_id)
        try:
            await db.rollback()
        except Exception:
            pass
