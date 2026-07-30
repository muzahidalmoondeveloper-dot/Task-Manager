from datetime import datetime, date
from typing import Optional
from pydantic import BaseModel, Field


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


# ─── Meeting ──────────────────────────────────────────────────────────────────

class MeetingCreate(BaseModel):
    title: str
    description: Optional[str] = None
    scheduled_at: datetime
    duration_minutes: int = 60
    meeting_type: str = "custom"
    organizer_id: Optional[int] = None
    participant_ids: list[int] = []


class MeetingUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    scheduled_at: Optional[datetime] = None
    duration_minutes: Optional[int] = None
    meeting_type: Optional[str] = None
    organizer_id: Optional[int] = None
    status: Optional[str] = None
    participant_ids: Optional[list[int]] = None


class MeetingOut(BaseModel):
    id: int
    title: str
    description: Optional[str] = None
    scheduled_at: datetime
    duration_minutes: int
    meeting_type: str
    status: str
    organizer_id: Optional[int] = None
    team_id: int
    organizer: Optional[UserRef] = None
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
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


class MeetingSummaryOut(BaseModel):
    total_duration_minutes: int
    completed_agenda: list[AgendaItemOut]
    pending_agenda: list[AgendaItemOut]
    decisions: list[DecisionOut]
    action_items: list[MeetingTaskOut]
    notes_count: int
