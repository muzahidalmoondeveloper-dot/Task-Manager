"""Closed taxonomy of ActivityLog actions (Task #8, extended by Task #8B) —
every activity recorded anywhere in the app must use one of these
constants, never an ad-hoc string, so the log stays a small, consistent,
queryable vocabulary instead of accumulating one-off strings per call
site.

Only actions for mutation flows that actually exist in this app are
defined here — no action was added speculatively for a feature that
doesn't exist (see the final report for the exact list of what's wired up
vs. intentionally not).

Categories (Task #8B) are derived from the action string's prefix before
the first "." — there is deliberately no separate DB `category` column;
`action` stays the single source of truth. See CATEGORY_PREFIXES below,
which both documents the mapping and is the whitelist the API's
`category` query filter is validated against.
"""

# ── Authentication (Task #8B) ────────────────────────────────────────────
# Only genuinely successful, credential-verified auth events are logged.
# Failed logins are deliberately NOT logged here (different privacy
# semantics — no authenticated actor, risk of storing attempted
# credentials/IPs) — that's left as a future, separate Security Log task.
AUTH_LOGIN = "auth.login"
AUTH_LOGOUT = "auth.logout"
AUTH_PASSWORD_CHANGED = "auth.password_changed"
AUTH_PASSWORD_RESET_COMPLETED = "auth.password_reset_completed"

# ── Task ──────────────────────────────────────────────────────────────────
TASK_CREATED = "task.created"
TASK_UPDATED = "task.updated"
TASK_DELETED = "task.deleted"
TASK_TIMER_STARTED = "task.timer_started"
TASK_TIMER_STOPPED = "task.timer_stopped"

# ── Project ───────────────────────────────────────────────────────────────
PROJECT_CREATED = "project.created"
PROJECT_UPDATED = "project.updated"
PROJECT_DELETED = "project.deleted"
PROJECT_MANAGER_ASSIGNED = "project.manager_assigned"
PROJECT_MANAGER_REMOVED = "project.manager_removed"

# ── Team ──────────────────────────────────────────────────────────────────
TEAM_CREATED = "team.created"
TEAM_UPDATED = "team.updated"
TEAM_DELETED = "team.deleted"
TEAM_MEMBER_ADDED = "team.member_added"
TEAM_MEMBER_REMOVED = "team.member_removed"

# ── User / access ─────────────────────────────────────────────────────────
USER_INVITED = "user.invited"
USER_CREATED = "user.created"
USER_UPDATED = "user.updated"
USER_ROLE_CHANGED = "user.role_changed"
USER_ACTIVATED = "user.activated"
USER_DEACTIVATED = "user.deactivated"

# ── Organization / admin (Task #8B) ─────────────────────────────────────
ORGANIZATION_UPDATED = "organization.updated"
ORGANIZATION_SETTINGS_UPDATED = "organization.settings_updated"

# ── Integrations (Task #8B) ──────────────────────────────────────────────
INTEGRATION_CONNECTED = "integration.connected"
INTEGRATION_DISCONNECTED = "integration.disconnected"

ENTITY_TASK = "task"
ENTITY_PROJECT = "project"
ENTITY_TEAM = "team"
ENTITY_USER = "user"
ENTITY_INVITATION = "invitation"
ENTITY_ORGANIZATION = "organization"
ENTITY_INTEGRATION = "integration"

ALL_ACTIONS = {
    AUTH_LOGIN, AUTH_LOGOUT, AUTH_PASSWORD_CHANGED, AUTH_PASSWORD_RESET_COMPLETED,
    TASK_CREATED, TASK_UPDATED, TASK_DELETED, TASK_TIMER_STARTED, TASK_TIMER_STOPPED,
    PROJECT_CREATED, PROJECT_UPDATED, PROJECT_DELETED, PROJECT_MANAGER_ASSIGNED, PROJECT_MANAGER_REMOVED,
    TEAM_CREATED, TEAM_UPDATED, TEAM_DELETED, TEAM_MEMBER_ADDED, TEAM_MEMBER_REMOVED,
    USER_INVITED, USER_CREATED, USER_UPDATED, USER_ROLE_CHANGED, USER_ACTIVATED, USER_DEACTIVATED,
    ORGANIZATION_UPDATED, ORGANIZATION_SETTINGS_UPDATED,
    INTEGRATION_CONNECTED, INTEGRATION_DISCONNECTED,
}

# Category → action-string prefix. Whitelist consumed by both the
# `GET /activity-logs?category=` filter (server-side, never arbitrary
# client-supplied SQL LIKE patterns) and the frontend filter bar. Adding a
# category here without also having real actions using that prefix would
# show an always-empty filter option, so keep this in lockstep with
# ALL_ACTIONS above.
CATEGORY_PREFIXES = {
    "authentication": "auth.",
    "users": "user.",
    "tasks": "task.",
    "projects": "project.",
    "teams": "team.",
    "organization": "organization.",
    "integrations": "integration.",
}


def category_for_action(action: str) -> str | None:
    """Best-effort category label for a stored action string, used by the
    API response so the frontend never has to re-derive prefix logic
    itself. Returns None for an action that (in theory) predates a
    category mapping — never raises."""
    prefix = action.split(".", 1)[0] + "." if "." in action else action
    for category, category_prefix in CATEGORY_PREFIXES.items():
        if prefix == category_prefix:
            return category
    return None
