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
    # Domain buildout (strict acceptance audit) — single-record create/status
    # changes for Issues, Rocks, KPI entries, and client-submitted requests
    # are the same risk shape as create_task/update_task_field: one record,
    # immediately visible on its own page, trivially correctable by hand if
    # wrong. AUTO tier, no preview/confirm needed.
    "create_issue": "R2",
    "update_issue_status": "R2",
    "create_rock": "R2",
    "update_rock_status": "R2",
    "record_kpi_value": "R2",
    "submit_client_request": "R2",
    "schedule_meeting": "R2",
    "update_meeting": "R2",
    "create_project": "R2",
    "create_team": "R2",
    "update_project": "R2",   # single record, same risk shape as update_issue_status/update_rock_status
    "update_team": "R2",      # rename/description only — NOT manager reassignment, see reassign_team_manager
    "create_knowledge_document": "R2",  # single record, trivially correctable/deletable by hand if wrong
    # Team-manager reassignment (architecture item 2) — a leadership/
    # personnel change with broader blast radius (cascades to who's
    # accountable for the team's whole task/rock/KPI workload) than any
    # single-field edit, so it gets the same CONFIRM treatment as
    # reassign_task rather than folding into update_team's AUTO tier.
    "reassign_team_manager": "R3",
    "generate_project_report": "R2",  # single record, same risk shape as any other domain create
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
