"""Policy Engine (spec Section 21-22) — the single, canonical place that
decides whether an org role may see/do something through the copilot. Both
the existing db_query sub-intent dispatch and the newer risk-gated write
tools consult this module, so the CLIENT-visibility rule only exists once."""

from app.core.org_roles import CLIENT

ALLOW = "allow"
DENY = "deny"
REQUIRE_CONFIRMATION = "require_confirmation"

# db_query sub-intents that surface internal-staff-only or org-wide data —
# a client asking one of these gets a polite refusal instead of an answer.
CLIENT_BLOCKED_SUB_INTENTS = {
    "user_count", "user_list", "user_by_role", "user_tasks",
    "team_count", "team_list", "team_members", "team_workload",
    "workload_summary", "project_count", "project_list", "project_by_status",
    "rock_list", "rock_by_team", "issue_list", "issue_open",
    "kpi_list", "kpi_progress", "meeting_list", "meeting_upcoming",
    # client_request_list is intentionally NOT blocked — a client asking
    # about the status of requests they submitted is exactly in scope.
}

CLIENT_REFUSAL_MESSAGE = (
    "I can only help with your own assigned project's status and tasks. "
    "For organization-wide information, please contact your project manager."
)

# Write tools that are simply never available through chat, for any role —
# spec Section 23's R6 (role/access change) and R7 (mass delete) rows.
_ALWAYS_BLOCKED_TOOLS = {"change_user_role", "delete_organization", "bulk_delete_all_tasks"}


def is_client_blocked_sub_intent(sub_intent: str) -> bool:
    return sub_intent in CLIENT_BLOCKED_SUB_INTENTS


def check_tool_policy(*, org_role: str, tool_name: str, is_bulk: bool = False) -> str:
    """Coarse RBAC pre-filter for a named write tool. Returns ALLOW / DENY /
    REQUIRE_CONFIRMATION. Fine-grained ABAC (e.g. "is this specific project
    assigned to this client") stays in the tool handler itself, since it
    needs the resolved target record, not just the role string."""
    if tool_name in _ALWAYS_BLOCKED_TOOLS:
        return DENY

    if org_role == CLIENT:
        # Clients have no write tools available through chat at all today —
        # every write tool this pass touches (task reassignment/deletion) is
        # internal staff work.
        return DENY

    return ALLOW
