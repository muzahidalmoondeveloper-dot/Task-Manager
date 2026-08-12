"""Strict, typed input contracts for every Domain Tool (architecture
Section 5 / 26). Every field the LLM (or any caller) can supply is declared
explicitly; `model_config = {"extra": "forbid"}` on the base class rejects
any parameter the tool doesn't know about instead of silently ignoring it
— "Unknown parameters should be rejected unless intentionally allowed."

These are deliberately separate from the existing app/schemas/task.py
request models: those describe the public HTTP API's contract; these
describe what the *chatbot* is allowed to pass into a tool, which is a
narrower, LLM-facing surface (e.g. no arbitrary `icon`/`priority` fields
the LLM has no reliable way to infer, and every write tool always requires
an explicit target rather than any implicit "current record" concept)."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class ToolInput(BaseModel):
    model_config = {"extra": "forbid"}


# ─── create_task ────────────────────────────────────────────────────────────

class CreateTaskItem(ToolInput):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    start_date: date | None = None
    due_date: date | None = None
    assignee_id: int | None = None
    project_id: int | None = None
    team_id: int | None = None
    status: str = "todo"


class CreateTaskInput(ToolInput):
    tasks: list[CreateTaskItem] = Field(min_length=1)


# ─── update_task_field (single-record, low-risk field edits) ──────────────────

class UpdateTaskFieldInput(ToolInput):
    task_id: int
    name: str | None = None
    status: str | None = None
    due_date: date | None = None


# ─── reassign_task (single-record, risk R3 — confirm) ──────────────────────────

class ReassignTaskInput(ToolInput):
    task_id: int
    assignee_id: int
    expected_version: str | None = None  # captured updated_at ISO string, for optimistic locking


# ─── delete_task_single (risk R3 — confirm) ────────────────────────────────────

class DeleteTaskInput(ToolInput):
    task_id: int


# ─── update_task_bulk (risk R4 — confirm) ──────────────────────────────────────

class UpdateTaskBulkInput(ToolInput):
    task_ids: list[int] = Field(min_length=1)
    status: str | None = None
    name: str | None = None
    due_date: date | None = None


# ─── convert_client_request_to_task (risk R3 — confirm) ───────────────────────

class ConvertClientRequestInput(ToolInput):
    request_id: int


# ─── create_issue / update_issue_status (issue domain, AUTO tier) ─────────────

class CreateIssueInput(ToolInput):
    title: str = Field(min_length=1, max_length=500)
    description: str | None = None
    team_id: int
    project_id: int | None = None
    assignee_id: int | None = None
    timeframe: str = "short-term"


class UpdateIssueStatusInput(ToolInput):
    issue_id: int
    status: str  # open | resolved
    resolution_plan: str | None = None


# ─── create_rock / update_rock_status (rock domain, AUTO tier) ────────────────

class CreateRockInput(ToolInput):
    title: str = Field(min_length=1, max_length=500)
    description: str | None = None
    team_id: int
    project_id: int | None = None
    owner_id: int | None = None
    due_date: date | None = None


class UpdateRockStatusInput(ToolInput):
    rock_id: int
    status: str  # backlog | on_track | at_risk | off_track | done


# ─── record_kpi_value (kpi domain, AUTO tier) ──────────────────────────────────

class RecordKpiValueInput(ToolInput):
    kpi_id: int
    value: float
    period_type: Literal["weekly", "monthly", "quarterly", "yearly"] = "weekly"
    period_start: date | None = None  # defaults to "today" (org-timezone-aware) if omitted
    note: str | None = None


# ─── submit_client_request (client_request domain, AUTO tier) ─────────────────

class SubmitClientRequestInput(ToolInput):
    title: str = Field(min_length=1, max_length=500)
    description: str | None = None
    project_id: int


# ─── schedule_meeting / update_meeting (meeting domain, AUTO tier) ─────────────
# architecture item 2 — "complete Meetings chatbot lifecycle capabilities":
# create/reschedule/cancel, plus start (in_progress) and end (completed).
# Still deliberately narrow — agenda items, participant roulette, etc. stay
# out of scope, same as this app's own "smart/AI-assisted scheduling —
# Phase 2" note on the Meeting model itself.

class ScheduleMeetingInput(ToolInput):
    title: str = Field(min_length=1, max_length=500)
    scheduled_at: datetime
    duration_minutes: int = 60
    team_id: int | None = None
    project_id: int | None = None
    location: str | None = None


class UpdateMeetingInput(ToolInput):
    meeting_id: int
    scheduled_at: datetime | None = None  # reschedule
    status: str | None = None  # e.g. "cancelled"


# ─── create_project / create_team (org-structure domain, AUTO tier) ───────────
# Deliberately create-only — renaming/restructuring/deleting a project or
# team is a much higher-blast-radius action than creating a new one — but
# per the master-prompt's item 2 ("complete Projects/Teams/Meetings chatbot
# lifecycle capabilities"), single-field edits (rename, description, status)
# are the same risk shape as every other domain's status-only update
# (update_issue_status/update_rock_status/update_meeting — all AUTO R2, one
# record, trivially correctable). True deletion, and reassigning a team's
# manager (a personnel/leadership change with wider blast radius than any
# single-field edit), stay out of this AUTO tier — team-manager reassignment
# is CONFIRM-tier (see reassign_team_manager below); deletion is still left
# to the existing UI, matching every other domain's "no delete via chat" rule.

class CreateProjectInput(ToolInput):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None


class CreateTeamInput(ToolInput):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    team_manager_id: int


class UpdateProjectInput(ToolInput):
    project_id: int
    name: str | None = None
    description: str | None = None
    # "archive" in this app's existing vocabulary (see
    # chat_service._PROJECT_STATUS_SYNONYMS) means status="cancelled" —
    # there's no separate archived state on the Project model.
    status: Literal["active", "paused", "completed", "cancelled"] | None = None


class UpdateTeamInput(ToolInput):
    team_id: int
    name: str | None = None
    description: str | None = None


class ReassignTeamManagerInput(ToolInput):
    team_id: int
    new_manager_id: int


# ─── create_knowledge_document (Knowledge/RAG domain, architecture item 1) ────

class CreateKnowledgeDocumentInput(ToolInput):
    title: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1)
    doc_type: str = "general"
    tags: list[str] | None = None


# ─── generate_project_report (Reporting domain, architecture item 3) ──────────
# AUTO-tier (R2) — creates one Report row and populates it from the
# project's current Rocks/KPIs/Tasks/Risks/Issues via the existing
# ReportGenerationService (the same engine the HTTP report module uses —
# not a separate, chat-only re-implementation). Single record, visible and
# deletable from the existing Reports page if wrong.

class GenerateProjectReportInput(ToolInput):
    project_id: int
    report_type: Literal["weekly", "monthly", "client", "team_performance"] = "monthly"
    title: str = Field(min_length=1, max_length=255)
