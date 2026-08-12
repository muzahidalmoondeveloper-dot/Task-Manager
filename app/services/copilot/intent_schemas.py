"""Structured intent/sub-intent contracts (architecture item 4 — "Structured
semantic understanding replacing the fragile overlapping intent pipeline
where appropriate").

Before this module existed, intent detection and db_query sub-intent
extraction both asked the LLM for free-form JSON and treated `intent`/
`sub_intent` as a bare string — a typo or drift in the LLM's output (e.g.
"creat_task" instead of "create_task") would silently fail every `==`
comparison in chat_service.py's if/elif dispatch chains and fall through to
_handle_general with **zero signal** anything went wrong; the model could
also invent a sub_intent value that was never in the allowed list.

These Pydantic models — validated via LLMGateway.generate_structured(), not
just generate_json()'s bare "is this JSON" check — turn "unknown value"
from a silent misroute into an explicit, loggable validation failure that
the same repair-retry/fallback machinery as malformed JSON now handles: the
LLM gets a second chance with the specific validation error, and if it
still can't produce a value from the closed set, callers get LLMSchemaError
instead of silently guessing.
"""

from typing import Literal

from pydantic import BaseModel, Field


class IntentDetectionResult(BaseModel):
    intent: Literal[
        "create_task", "list_tasks", "update_task", "delete_task",
        "analyze_text", "db_query", "convert_request_to_task", "general",
        # Domain buildout (strict acceptance audit) — Issue/Rock/KPI/
        # Client-Request write intents.
        "manage_issue", "manage_rock", "record_kpi", "submit_client_request", "manage_meeting",
        "create_project", "create_team",
    ]
    # Defaults to 0.0 (uncertain) when the model omits it entirely — no
    # repair round-trip needed for that common, harmless case. An in-range
    # numeric value must still genuinely be present to mean anything other
    # than "uncertain" (architecture Section 13 — "invalid confidence must
    # not become 1.0"); an out-of-range or non-numeric value fails the
    # ge/le|type constraint and IS repaired (the model gets one chance to
    # supply a real value) before falling back to the same 0.0/general
    # default chat_service.py's except-block always lands on.
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


# The full closed set of db_query sub-intents chat_service.py's dispatch
# chain understands (see _handle_db_query_impl) — kept here as the single
# source of truth for what's "known" so the LLM's output can be validated
# against exactly what the dispatcher can actually do, not a vaguer prose
# description of it.
DbSubIntent = Literal[
    "user_count", "user_list", "user_by_role", "user_tasks",
    "task_by_status", "task_overdue", "task_by_project", "task_by_team", "task_due_soon",
    "project_count", "project_list", "project_by_status", "project_progress",
    "team_count", "team_list", "team_members", "team_workload", "workload_summary",
    "rock_list", "rock_by_team", "issue_list", "issue_open",
    "kpi_list", "kpi_progress", "meeting_list", "meeting_upcoming",
    "client_request_list", "my_scoreboard", "search_everything",
]


class DbQueryExtraction(BaseModel):
    sub_intent: DbSubIntent
    user_name: str | None = None
    project_name: str | None = None
    team_name: str | None = None
    status: str | None = None
    role: str | None = None
    days_ahead: int | None = None
    target_self: bool = False
    # For my_scoreboard.
    period: Literal["this_week", "this_month", "this_quarter", "this_year"] = "this_month"


# ─── planner.py / memory.py / topics.py structured outputs (security gap #3
# from the strict acceptance audit — "unsafe/free-form LLM parsing" in the
# three supporting modules item 5's original pass missed) ──────────────────

class GoalSplitResult(BaseModel):
    steps: list[str] = Field(min_length=1)


class MemoryExtraction(BaseModel):
    has_preference: bool = False
    key: str = ""
    value: str = ""


class TopicUpdateResult(BaseModel):
    action: Literal["continue", "new_topic"]
    title: str = "General"
    summary: str = ""
    open_items: list[str] = Field(default_factory=list)


# ─── Domain buildout extraction schemas (Issue/Rock/KPI/Client-Request) ───────

class ManageIssueExtraction(BaseModel):
    action: Literal["create", "update_status"]
    title: str | None = None  # required for "create"
    team_name: str | None = None  # required for "create"
    project_name: str | None = None
    issue_reference: str | None = None  # ID or title fragment, required for "update_status"
    status: Literal["open", "resolved"] | None = None
    resolution_plan: str | None = None


class ManageRockExtraction(BaseModel):
    action: Literal["create", "update_status"]
    title: str | None = None
    team_name: str | None = None
    project_name: str | None = None
    rock_reference: str | None = None
    status: Literal["backlog", "on_track", "at_risk", "off_track", "done"] | None = None
    due_date: str | None = None  # ISO date string or null


class RecordKpiExtraction(BaseModel):
    kpi_reference: str  # ID or title fragment
    value: float
    period_type: Literal["weekly", "monthly", "quarterly", "yearly"] = "weekly"
    note: str | None = None


class SubmitClientRequestExtraction(BaseModel):
    title: str
    description: str | None = None
    project_name: str | None = None


class CreateProjectExtraction(BaseModel):
    name: str
    description: str | None = None


class CreateTeamExtraction(BaseModel):
    name: str
    description: str | None = None
    team_manager_name: str  # required — every team must have a manager


class CreateKnowledgeDocumentExtraction(BaseModel):
    """architecture item 1 — Knowledge/RAG retrieval, write side."""
    title: str
    content: str
    doc_type: str = "general"
    tags: list[str] = Field(default_factory=list)


class ManageProjectExtraction(BaseModel):
    """architecture item 2 — Projects lifecycle. AUTO-tier: name/description/
    status only (status="cancelled" for "archive this project")."""
    project_reference: str  # ID or name fragment
    name: str | None = None
    description: str | None = None
    status: Literal["active", "paused", "completed", "cancelled"] | None = None


class ManageTeamExtraction(BaseModel):
    """architecture item 2 — Teams lifecycle. "update" (rename/description,
    AUTO) vs. "reassign_manager" (a leadership change, CONFIRM-tier)."""
    action: Literal["update", "reassign_manager"]
    team_reference: str  # ID or name fragment
    name: str | None = None
    description: str | None = None
    new_manager_name: str | None = None  # required for reassign_manager


class GenerateReportExtraction(BaseModel):
    """architecture item 3 — Reporting domain."""
    project_reference: str  # ID or name fragment
    report_type: Literal["weekly", "monthly", "client", "team_performance"] = "monthly"
    title: str


class ManageMeetingExtraction(BaseModel):
    action: Literal["schedule", "update"]
    title: str | None = None  # required for "schedule"
    scheduled_at: str | None = None  # ISO datetime string
    duration_minutes: int | None = None
    team_name: str | None = None
    project_name: str | None = None
    location: str | None = None
    meeting_reference: str | None = None  # ID or title fragment, required for "update"
    status: Literal["scheduled", "in_progress", "completed", "cancelled"] | None = None


# ─── Canonical, language-agnostic reference classification ────────────────────
#
# Language-agnostic refactor: this replaces chat_service.py's old approach of
# emitting a raw reference STRING from the LLM, then pattern-matching that
# string in the backend against hardcoded, per-language word lists (English
# ordinals, then a parallel Bengali/Banglish word list, and so on for every
# language the app wanted to support — a list that could never be complete
# and had to be hand-maintained per language).
#
# Instead, the LLM — which already understands whatever language/script the
# user wrote in — does the linguistic classification itself, at extraction
# time, and reduces it to ONE of three deterministic, language-independent
# shapes below. The backend (chat_service.py's _resolve_task_reference())
# then only ever branches on `reference_type`; it never inspects the
# reference text for language-specific ordinal/pronoun words. A user writing
# "the second one", "দ্বিতীয়টা", "dwitiyo", "le deuxième", or any other
# language's equivalent all produce the identical canonical
# {"reference_type": "ordinal", "ordinal_position": 2} — the backend code
# path is exactly the same regardless of which language produced it.
_REFERENCE_TYPE = Literal["all", "ordinal", "deictic", "explicit"]


class TaskUpdateFields(BaseModel):
    name: str | None = None
    status: str | None = None
    due_date: str | None = None
    assignee_name: str | None = None


class UpdateTaskExtraction(BaseModel):
    reference_type: _REFERENCE_TYPE = "explicit"
    # "explicit" only — an ID number or a fragment of the task's title.
    task_reference: str | None = None
    # "ordinal" only — 1-based position (1=first, 2=second, ...); -1 means
    # "the last one". Resolved against the session's stored previous
    # result-set (see chat_service._resolve_positional_task_id).
    ordinal_position: int | None = None
    updates: TaskUpdateFields = Field(default_factory=TaskUpdateFields)


class DeleteTaskExtraction(BaseModel):
    reference_type: _REFERENCE_TYPE = "explicit"
    task_reference: str | None = None
    ordinal_position: int | None = None
