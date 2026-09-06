from datetime import datetime

from pydantic import BaseModel, Field


class TaskTimeState(BaseModel):
    """Authoritative, compact time-tracking state for one task — everything
    the task UI needs without downloading full session history.

    `tracked_time_seconds` is the task's true total Working Time as of this
    read: the DB-summed total of every completed session on this task
    (across ALL users who ever worked it) PLUS the live elapsed time of
    EVERY currently-active session on this task (again, across all users —
    #7A guarantees one active timer per user, never one active timer per
    task, so more than one person can be actively timing the same task at
    once — see `active_timer_count` and the Task List Working Time
    follow-up spec). This is deliberately the same "task total" definition
    used everywhere else Working Time is shown (task lists, Project
    Working Time) — never a private, current-user-only number — so nothing
    else in the UI shows an inconsistent smaller total.

    `is_active` / `active_started_at` describe only the *caller's own*
    session (never another user's) — this is what actually drives the
    Start/Stop button in TaskTimeTracker: a user must never see a "Stop"
    button for a timer someone else started. `active_timer_count` is the
    additive, privacy-safe way to know "how many sessions total are
    currently running on this task" without exposing whose they are (Task
    List Working Time follow-up, Phase 34: no per-user breakdown, ever).
    """

    task_id: int
    tracked_time_seconds: int
    is_active: bool
    # Only the current user's own active session — never exposes whether
    # some other user has one running (out of scope: this isn't a
    # per-member breakdown, see Phase 13 of the spec).
    active_started_at: datetime | None = None
    # Additive (Task List Working Time follow-up): total active sessions
    # on this task across ALL users, never broken down by who — lets the
    # frontend know its live baseline should tick at
    # `active_timer_count` seconds of Working Time per real second.
    active_timer_count: int = 0
    # Additive (Assignee-Only Timer Control follow-up): lets
    # TaskTimeTracker decide for itself whether to render Start/Stop at
    # all, by comparing this against the authenticated viewer's own id —
    # the same public, already-exposed value TaskDetailRead carries
    # (never a new privacy surface). Timer *control* is assignee-only
    # regardless of role (see app.api.routes.tasks.can_control_task_timer);
    # this field is what lets the frontend match that rule without a
    # second round-trip for the full task.
    assignee_id: int | None = None

    model_config = {"from_attributes": True}


class TaskTimeSummaryItem(BaseModel):
    """One task's entry in a bulk Working Time response — canonical
    numeric seconds only, never a pre-formatted string (the frontend picks
    compact vs. live-ticking display, see the Task List Working Time
    follow-up spec's formatter split)."""

    working_time_seconds: int
    active_timer_count: int
    # Additive (Start/Stop-from-list follow-up): whether the AUTHENTICATED
    # caller (never a client-supplied user_id) is the one running one of
    # this task's active sessions. Deliberately separate from
    # `active_timer_count > 0` — that describes the TASK aggregate (could
    # be entirely other users), this describes the CALLER'S OWN timer and
    # is the only thing that may ever decide whether a list row shows
    # "Stop" (never "Start" plus disabled, and never inferred from the
    # aggregate count — see the spec's explicit warning about this)."""
    current_user_is_active: bool = False


class TaskTimeSummariesRequest(BaseModel):
    """Bulk Working Time lookup for a Task list/table/card view — ONE
    request for however many tasks are currently visible, never one
    `GET /tasks/{id}/time` per row. Bounded so a client can't force an
    unbounded aggregation query with an arbitrarily large id list."""

    task_ids: list[int] = Field(default_factory=list, max_length=500)


class TaskTimeSummariesResponse(BaseModel):
    calculated_at: datetime
    # Keyed by task id as a string (JSON object keys are always strings) —
    # only task_ids the caller is actually visible to see one; a
    # requested-but-not-visible/nonexistent/other-org task_id is silently
    # omitted, never an error (matches how a single 403/404 elsewhere in
    # this app wouldn't make sense for "one of several ids in a batch").
    items: dict[str, TaskTimeSummaryItem]

    # Additive (Start/Stop-from-list follow-up): #7A allows only ONE active
    # timer per user across the whole organization, so a list needs to know
    # up front whether the caller is already timing something — anywhere,
    # not just among the requested task_ids — to disable every OTHER
    # task's Start button instead of letting the user discover a 409 only
    # after clicking. This is exclusively the caller's OWN state, derived
    # from the authenticated TenantContext, never a client-supplied
    # user_id, and never reveals anything about other users' timers.
    current_user_has_active_timer: bool = False
    # The task_id of the caller's own active session, IF it is one of the
    # task_ids visible in this response's `items` — otherwise null, even
    # when `current_user_has_active_timer` is true (e.g. their active
    # timer is on a task that isn't part of this particular list/page).
    # This is never a privacy concern (it is always the caller's own
    # task), but a list has no use for an id it has no other data for, and
    # withholding it here keeps this field's meaning exactly
    # "the row in *this* response you should show Stop on", rather than
    # a general-purpose task lookup.
    current_user_active_task_id: int | None = None


class ProjectTimeSummary(BaseModel):
    """Project Working Time (#7B) — a derived aggregate, never persisted.
    `working_time_seconds` already includes every currently-active
    session's live elapsed time as of `calculated_at`. `active_timer_count`
    lets the frontend keep the displayed total increasing between requests
    (`elapsed_since_calculated_at * active_timer_count`) without polling —
    it deliberately reveals nothing about *which* users those sessions
    belong to (no per-member breakdown; see Phase 13/20 of the spec)."""

    project_id: int
    working_time_seconds: int
    active_timer_count: int
    calculated_at: datetime
