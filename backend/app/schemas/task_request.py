from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator

from app.schemas.task import TASK_PRIORITIES
from app.schemas.user import UserRead


class TaskRequestCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    description: str | None = None


class TaskRequestConvert(BaseModel):
    team_id: int
    assignee_id: int | None = None
    priority: str = "medium"
    due_date: date | None = None

    @field_validator("priority")
    @classmethod
    def validate_priority(cls, value: str) -> str:
        if value not in TASK_PRIORITIES:
            raise ValueError("Invalid task priority.")
        return value


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
