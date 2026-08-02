# Single source of truth for Client Onboarding status/type strings.
# Mirrors the vocabulary in the Client Onboarding workflow spec (Foundation phase).

ONBOARDING_STATUSES = {
    "draft",
    "invited",
    "client_registered",
    "in_progress",
    "waiting_for_client",
    "under_review",
    "changes_requested",
    "waiting_for_internal_team",
    "ready_for_approval",
    "completed",
    "rejected",
    "cancelled",
    "archived",
}

# Once an onboarding reaches one of these it's done — no further automatic
# status derivation happens once here.
ONBOARDING_TERMINAL_STATUSES = {"completed", "rejected", "cancelled", "archived"}
ONBOARDING_ACTIVE_STATUSES = ONBOARDING_STATUSES - ONBOARDING_TERMINAL_STATUSES

# The specific statuses that block starting a second onboarding for the same
# org+client+project — a new cycle is only allowed once the existing record
# is completed, cancelled, or archived (notably: NOT "rejected", "invited",
# "client_registered", or "waiting_for_internal_team" — those don't block).
ONBOARDING_DUPLICATE_BLOCKING_STATUSES = {
    "draft",
    "in_progress",
    "under_review",
    "changes_requested",
    "waiting_for_client",
    "ready_for_approval",
}

STEP_STATUSES = {
    "not_started",
    "in_progress",
    "submitted",
    "under_review",
    "changes_requested",
    "approved",
    "completed",
    "skipped",
}

STEP_TYPES = {
    "information_form",
    "questionnaire",
    "document_upload",
    "agreement_acceptance",
    "payment",
    "meeting",
    "manual_task",
    "approval",
    "custom_step",
}

# Step statuses that count as "done" for progress-percentage purposes.
STEP_STATUSES_COMPLETE = {"approved", "completed", "skipped"}

# Step statuses a client may still edit their submission/documents under —
# either they haven't submitted yet, or a reviewer kicked it back to them.
STEP_STATUSES_CLIENT_EDITABLE = {"not_started", "in_progress", "changes_requested"}

FORM_FIELD_TYPES = {
    "short_text",
    "long_text",
    "email",
    "phone",
    "url",
    "number",
    "date",
    "dropdown",
    "checkbox",
    "multi_select",
}

DOCUMENT_STATUSES = {
    "uploaded",
    "under_review",
    "approved",
    "rejected",
    "replaced",
}

CHANGE_REQUEST_STATUSES = {"open", "resolved"}
