from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.task import TASK_PRIORITIES
from app.schemas.user import UserRead


class TaskRequestCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    description: str | None = None


class TaskRequestConvert(BaseModel):
    """Client Task Request -> Task conversion follow-up. Exactly two
    explicit conversion modes — never inferred from whether `team_id`
    happens to be present, and there is deliberately no `assignee_id`
    field at all: a Project Manager (or any other staff converting a
    request) picks EITHER themself ("self") OR a Team ("team"); the Team
    Manager decides the individual owner afterwards. See
    app.api.routes.task_requests.convert_task_request for the
    authoritative server-side enforcement of what each mode actually
    persists — this schema only rejects a contradictory payload shape."""
    conversion_mode: Literal["self", "team"]
    team_id: int | None = None
    priority: str = "medium"
    due_date: date | None = None

    # extra="forbid": a submitted `assignee_id` (or any other unknown
    # field) is rejected outright (422) rather than silently ignored —
    # there is no ambiguity about whether a forged individual assignee
    # was "accepted but dropped" vs. never a legal input in the first
    # place.
    model_config = {"extra": "forbid"}

    @field_validator("priority")
    @classmethod
    def validate_priority(cls, value: str) -> str:
        if value not in TASK_PRIORITIES:
            raise ValueError("Invalid task priority.")
        return value

    @model_validator(mode="after")
    def validate_mode_shape(self) -> "TaskRequestConvert":
        if self.conversion_mode == "self" and self.team_id is not None:
            raise ValueError("team_id must not be provided when conversion_mode is 'self'.")
        if self.conversion_mode == "team" and self.team_id is None:
            raise ValueError("team_id is required when conversion_mode is 'team'.")
        return self


class TaskRequestReject(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


class TaskRequestOut(BaseModel):
    id: int
    project_id: int
    title: str
    description: str | None = None
    status: str
    converted_task_id: int | None = None
    reviewed_at: datetime | None = None
    created_at: datetime

    submitted_by: UserRead | None = None
    reviewed_by: UserRead | None = None

    model_config = {"from_attributes": True}


class ClientTaskRequestOut(TaskRequestOut):
    """TaskRequestOut plus the project name — used by the Users page's
    client detail view, which lists a client's requests across every
    project they belong to (the plain project_id alone isn't enough there
    since there's no single project context on screen)."""
    project_name: str | None = None
