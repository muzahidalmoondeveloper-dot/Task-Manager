"""Risk Decision Engine (spec Section 23) — a static risk table per write
action. Read tools are never in this table (they're always R0/R1, handled
by simply not routing through change-sets at all)."""

AUTO = "auto"                 # execute immediately, no preview
CONFIRM = "confirm"           # build a change set, wait for explicit confirmation
BLOCK = "block"               # never executable through chat

# Mirrors spec Section 23.1's R0-R7 table, scoped to the write actions this
# app's chat currently supports. Single-record edits stay AUTO (today's
# existing UX for "mark task 12 done" is preserved); anything that reassigns
# a person, deletes a record, or touches many records at once requires
# confirmation; mass-deleting every task in the org is blocked outright
# (R7 — "mass delete... block from chatbot") rather than merely confirmed,
# since a chat confirmation is too thin a safeguard for that blast radius.
RISK_TABLE: dict[str, str] = {
    "create_task": "R2",
    "update_task_field": "R2",          # name / status / due_date on one task
    "reassign_task": "R3",              # single-task assignee change
    "delete_task_single": "R3",
    "update_task_bulk": "R4",           # "mark all tasks as done" etc.
    "delete_task_bulk": "R7",
    "convert_client_request_to_task": "R3",  # creates a real task from client input
}

_RISK_ACTION = {
    "R0": AUTO, "R1": AUTO, "R2": AUTO,
    "R3": CONFIRM, "R4": CONFIRM, "R5": CONFIRM,
    "R6": BLOCK, "R7": BLOCK,
}


def risk_level(tool_name: str) -> str:
    return RISK_TABLE.get(tool_name, "R2")


def risk_action(tool_name: str) -> str:
    return _RISK_ACTION.get(risk_level(tool_name), AUTO)


# Multi-Level Approvals (spec Section 47, bounded) — a team_manager (not an
# owner/admin) initiating a bulk update is the one concrete scenario in this
# app where self-confirmation isn't enough oversight: bulk edits can touch
# tasks well outside what a single manager should unilaterally change, so it
# needs an admin's sign-off instead of just the requester's own confirm.
def requires_admin_approval(tool_name: str, org_role: str) -> bool:
    from app.core.org_roles import TEAM_MANAGER

    return tool_name == "update_task_bulk" and org_role == TEAM_MANAGER
