from datetime import datetime, date
from typing import Optional
from pydantic import BaseModel, Field, field_validator

from app.core.meeting_constants import MEETING_PRIORITIES, MEETING_RECURRENCES, MEETING_VISIBILITIES


class UserRef(BaseModel):
    id: int
    full_name: Optional[str] = None
    email: str
    model_config = {"from_attributes": True}


class TaskRef(BaseModel):
    id: int
    name: str
    status: str
    priority: str
    due_date: Optional[date] = None
    model_config = {"from_attributes": True}


class ProjectRef(BaseModel):
    id: int
    name: str
    model_config = {"from_attributes": True}


class IssueRef(BaseModel):
    id: int
    title: str
    status: str
    priority: int
    model_config = {"from_attributes": True}


# ─── Participant ───────────────────────────────────────────────────────────────

class MeetingParticipantOut(BaseModel):
    id: int
    user_id: Optional[int] = None
    user: Optional[UserRef] = None
    selected: bool = True
    joined_at: Optional[datetime] = None
    spoken_at: Optional[datetime] = None
    skipped: bool = False
    score: Optional[float] = None
    score_note: Optional[str] = None
    scored_at: Optional[datetime] = None
    model_config = {"from_attributes": True}


class ParticipantJoinUpdate(BaseModel):
    joined: bool


class ParticipantScoreUpdate(BaseModel):
    score: float = Field(ge=1, le=10)
    note: Optional[str] = None



class SelectSpeakerRequest(BaseModel):
    user_id: int


# ─── Agenda ───────────────────────────────────────────────────────────────────

class AgendaItemCreate(BaseModel):
    title: str
    duration_minutes: Optional[int] = None
    sort_order: int = 0
    presenter_id: Optional[int] = None


class AgendaItemUpdate(BaseModel):
    title: Optional[str] = None
    duration_minutes: Optional[int] = None
    status: Optional[str] = None
    sort_order: Optional[int] = None
    presenter_id: Optional[int] = None


class AgendaItemOut(BaseModel):
    id: int
    title: str
    duration_minutes: Optional[int] = None
    status: str
    sort_order: int
    presenter_id: Optional[int] = None
    presenter: Optional[UserRef] = None
    model_config = {"from_attributes": True}


class AgendaReorderItem(BaseModel):
    id: int
    sort_order: int


# ─── Notes ────────────────────────────────────────────────────────────────────

class NoteCreate(BaseModel):
    content: str


class NoteUpdate(BaseModel):
    content: str


class NoteOut(BaseModel):
    id: int
    content: str
    created_by_id: Optional[int] = None
    created_by: Optional[UserRef] = None
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


# ─── Decisions ────────────────────────────────────────────────────────────────

class DecisionCreate(BaseModel):
    content: str


class DecisionOut(BaseModel):
    id: int
    content: str
    author_id: Optional[int] = None
    author: Optional[UserRef] = None
    created_at: datetime
    model_config = {"from_attributes": True}


# ─── Meeting Tasks ────────────────────────────────────────────────────────────

class MeetingTaskOut(BaseModel):
    id: int
    task_id: int
    agenda_item_id: Optional[int] = None
    task: Optional[TaskRef] = None
    model_config = {"from_attributes": True}


class MeetingCreateTask(BaseModel):
    name: str
    assignee_id: Optional[int] = None
    due_date: Optional[str] = None
    priority: str = "medium"
    agenda_item_id: Optional[int] = None


class MeetingLinkTask(BaseModel):
    """Attach an *existing* task to the meeting — distinct from
    MeetingCreateTask above, which creates a brand-new one. Backs the
    Linked Task Integration flow (plan section 7): suggested overdue/
    high-priority tasks get linked in, not duplicated."""
    task_id: int
    agenda_item_id: Optional[int] = None


# ─── Meeting ──────────────────────────────────────────────────────────────────

class MeetingCreate(BaseModel):
    title: str
    description: Optional[str] = None
    objective: Optional[str] = None
    scheduled_at: datetime
    duration_minutes: int = 60
    meeting_type: str = "custom"
    priority: str = "medium"
    visibility: str = "team"
    recurrence: str = "none"
    location: Optional[str] = None
    # Meetings are not team-specific — omit for an org-wide meeting (visible
    # per `visibility` below); set to scope it to one team (shows on that
    # team's own Meetings tab too, and narrows suggested tasks/issues to it).
    team_id: Optional[int] = None
    project_id: Optional[int] = None
    organizer_id: Optional[int] = None
    participant_ids: list[int] = []
    # Agenda Builder (plan section 3, center panel) — submitted together with
    # the rest of the meeting at creation time instead of requiring a
    # separate round-trip per section afterward.
    agenda_items: list[AgendaItemCreate] = []
    # Linked Task Integration (plan section 7) — existing tasks (e.g.
    # suggested overdue/high-priority ones) attached at creation time.
    linked_task_ids: list[int] = []

    @field_validator("priority")
    @classmethod
    def validate_priority(cls, v: str) -> str:
        if v not in MEETING_PRIORITIES:
            raise ValueError(f"priority must be one of: {', '.join(sorted(MEETING_PRIORITIES))}")
        return v

    @field_validator("visibility")
    @classmethod
    def validate_visibility(cls, v: str) -> str:
        if v not in MEETING_VISIBILITIES:
            raise ValueError(f"visibility must be one of: {', '.join(sorted(MEETING_VISIBILITIES))}")
        return v

    @field_validator("recurrence")
    @classmethod
    def validate_recurrence(cls, v: str) -> str:
        if v not in MEETING_RECURRENCES:
            raise ValueError(f"recurrence must be one of: {', '.join(sorted(MEETING_RECURRENCES))}")
        return v


class MeetingUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    objective: Optional[str] = None
    scheduled_at: Optional[datetime] = None
    duration_minutes: Optional[int] = None
    meeting_type: Optional[str] = None
    priority: Optional[str] = None
    visibility: Optional[str] = None
    recurrence: Optional[str] = None
    location: Optional[str] = None
    team_id: Optional[int] = None
    project_id: Optional[int] = None
    organizer_id: Optional[int] = None
    status: Optional[str] = None
    participant_ids: Optional[list[int]] = None

    @field_validator("priority")
    @classmethod
    def validate_priority(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in MEETING_PRIORITIES:
            raise ValueError(f"priority must be one of: {', '.join(sorted(MEETING_PRIORITIES))}")
        return v

    @field_validator("visibility")
    @classmethod
    def validate_visibility(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in MEETING_VISIBILITIES:
            raise ValueError(f"visibility must be one of: {', '.join(sorted(MEETING_VISIBILITIES))}")
        return v

    @field_validator("recurrence")
    @classmethod
    def validate_recurrence(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in MEETING_RECURRENCES:
            raise ValueError(f"recurrence must be one of: {', '.join(sorted(MEETING_RECURRENCES))}")
        return v


class MeetingOut(BaseModel):
    id: int
    title: str
    description: Optional[str] = None
    objective: Optional[str] = None
    scheduled_at: datetime
    duration_minutes: int
    meeting_type: str
    status: str
    priority: str
    visibility: str
    recurrence: str
    location: Optional[str] = None
    organizer_id: Optional[int] = None
    team_id: Optional[int] = None
    # Populated by the route (not a model relationship) when listing —
    # meetings aren't team-specific, so the team name (when there is one)
    # is useful context in a flat, cross-team list.
    team_name: Optional[str] = None
    project_id: Optional[int] = None
    organizer: Optional[UserRef] = None
    project: Optional[ProjectRef] = None
    checkin_current_participant_id: Optional[int] = None
    current_agenda_item_id: Optional[int] = None
    participants: list[MeetingParticipantOut] = []
    agenda_items: list[AgendaItemOut] = []
    notes: list[NoteOut] = []
    decisions: list[DecisionOut] = []
    meeting_tasks: list[MeetingTaskOut] = []
    started_at: Optional[datetime] = None
    paused_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    is_recording: bool = False
    transcript_text: Optional[str] = None
    transcript_analyzed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


# ─── Recording / Transcript / AI task extraction ───────────────────────────────

class TranscriptSubmit(BaseModel):
    """The client-side (browser) speech-to-text result, sent once recording
    stops. Always the full transcript accumulated so far (not just the
    latest delta) — the extraction endpoint re-analyzes the whole thing and
    relies on title-based de-duplication against already-created meeting
    to-dos, so re-submitting an extended transcript after a second
    record/stop cycle in the same meeting is safe."""
    text: str = Field(min_length=1)


class ExtractedTaskSummary(BaseModel):
    title: str
    assignee_id: Optional[int] = None
    assignee_name: Optional[str] = None
    due_date: Optional[date] = None


class TranscriptAnalyzeResult(BaseModel):
    meeting: MeetingOut
    tasks_created: list[ExtractedTaskSummary]


class MeetingSummaryOut(BaseModel):
    total_duration_minutes: int
    completed_agenda: list[AgendaItemOut]
    pending_agenda: list[AgendaItemOut]
    decisions: list[DecisionOut]
    action_items: list[MeetingTaskOut]
    notes_count: int


# ─── Meeting Templates ("Save Meeting Template") ───────────────────────────────

class TemplateAgendaSection(BaseModel):
    title: str
    duration_minutes: Optional[int] = None


class MeetingTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    meeting_type: str = "custom"
    description: Optional[str] = None
    default_duration_minutes: int = 60
    agenda_sections: list[TemplateAgendaSection] = []


class MeetingTemplateOut(BaseModel):
    id: int
    name: str
    meeting_type: str
    description: Optional[str] = None
    default_duration_minutes: int
    agenda_sections: list[TemplateAgendaSection]
    is_builtin: bool
    created_by: Optional[UserRef] = None
    created_at: datetime
    model_config = {"from_attributes": True}


# ─── Suggested tasks/issues (plan section 7 — Linked Task Integration) ────────

class SuggestedTasksOut(BaseModel):
    overdue: list[TaskRef] = []
    high_priority: list[TaskRef] = []
    unresolved_issues: list[IssueRef] = []


# ─── Live reactions (ephemeral — Redis-backed, never persisted to Postgres) ───

REACTION_EMOJIS = {"👍", "👏", "❤️", "😊"}


class MeetingReactionCreate(BaseModel):
    emoji: str

    @field_validator("emoji")
    @classmethod
    def validate_emoji(cls, v: str) -> str:
        if v not in REACTION_EMOJIS:
            raise ValueError(f"emoji must be one of: {', '.join(sorted(REACTION_EMOJIS))}")
        return v


class MeetingReactionOut(BaseModel):
    id: int
    emoji: str
    user_id: int
    ts: float
