"""Closed taxonomy of ActivityLog actions (Task #8) — every activity
recorded anywhere in the app must use one of these constants, never an
ad-hoc string, so the log stays a small, consistent, queryable vocabulary
instead of accumulating one-off strings per call site.

Only actions for mutation flows that actually exist in this app are
defined here — no action was added speculatively for a feature that
doesn't exist (see the final report for the exact list of what's wired up
vs. intentionally not).
"""

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
USER_UPDATED = "user.updated"
USER_ROLE_CHANGED = "user.role_changed"

ENTITY_TASK = "task"
ENTITY_PROJECT = "project"
ENTITY_TEAM = "team"
ENTITY_USER = "user"

ALL_ACTIONS = {
    TASK_CREATED, TASK_UPDATED, TASK_DELETED, TASK_TIMER_STARTED, TASK_TIMER_STOPPED,
    PROJECT_CREATED, PROJECT_UPDATED, PROJECT_DELETED, PROJECT_MANAGER_ASSIGNED, PROJECT_MANAGER_REMOVED,
    TEAM_CREATED, TEAM_UPDATED, TEAM_DELETED, TEAM_MEMBER_ADDED, TEAM_MEMBER_REMOVED,
    USER_UPDATED, USER_ROLE_CHANGED,
}
