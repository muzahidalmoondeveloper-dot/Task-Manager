from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.project import ProjectRead
from app.schemas.team import TeamRead
from app.schemas.user import UserRead


TASK_STATUSES = {"todo", "in_progress", "pending_review", "done"}
TASK_PRIORITIES = {"low", "medium", "high"}


class TaskCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    icon: str | None = None
    start_date: date | None = None
    due_date: date | None = None
    assignee_id: int | None = None
    project_id: int | None = None
    team_id: int | None = None
    status: str = "todo"
    priority: str = "medium"

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in TASK_STATUSES:
            raise ValueError("Invalid task status.")
        return value

    @field_validator("priority")
    @classmethod
    def validate_priority(cls, value: str) -> str:
        if value not in TASK_PRIORITIES:
            raise ValueError("Invalid task priority.")
        return value

    @model_validator(mode="after")
    def validate_dates(self):
        if self.start_date and self.due_date and self.due_date < self.start_date:
            raise ValueError("Due date cannot be before start date.")
        return self


class TaskUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    icon: str | None = None
    start_date: date | None = None
    due_date: date | None = None
    assignee_id: int | None = None
    project_id: int | None = None
    team_id: int | None = None
    status: str | None = None
    priority: str | None = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str | None) -> str | None:
        if value is not None and value not in TASK_STATUSES:
            raise ValueError("Invalid task status.")
        return value

    @field_validator("priority")
    @classmethod
    def validate_priority(cls, value: str | None) -> str | None:
        if value is not None and value not in TASK_PRIORITIES:
            raise ValueError("Invalid task priority.")
        return value

    @model_validator(mode="after")
    def validate_dates(self):
        if self.start_date and self.due_date and self.due_date < self.start_date:
            raise ValueError("Due date cannot be before start date.")
        return self


class TaskStatusUpdate(BaseModel):
    status: str

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in TASK_STATUSES:
            raise ValueError("Invalid task status.")
        return value


class TaskRead(BaseModel):
    id: int
    name: str
    icon: str | None = None
    start_date: date | None = None
    due_date: date | None = None
    status: str
    priority: str = "medium"
    assignee_id: int | None = None
    project_id: int | None = None
    team_id: int | None = None
    created_by_id: int

    completed_by_id: int | None = None
    completed_at: datetime | None = None
    reviewed_by_id: int | None = None
    reviewed_at: datetime | None = None
    review_note: str | None = None

    created_at: datetime
    updated_at: datetime

    model_config = {
        "from_attributes": True,
    }


class TaskDetailRead(TaskRead):
    assignee: UserRead | None = None
    project: ProjectRead | None = None
    team: TeamRead | None = None


class AssignBackRequest(BaseModel):
    note: str | None = Field(default=None, max_length=1000)
