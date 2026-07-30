from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator


SUGGESTION_STATUSES = {"pending", "approved", "rejected"}
CONFIDENCE_VALUES = {"low", "medium", "high"}


class AnalyzeSourceRequest(BaseModel):
    source_type: str
    source_id: int

    @field_validator("source_type")
    @classmethod
    def validate_source_type(cls, value: str) -> str:
        if value not in {"email", "transcript"}:
            raise ValueError("source_type must be email or transcript.")
        return value


class ExtractedTask(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str | None = None
    suggested_start_date: date | None = None
    suggested_due_date: date | None = None
    suggested_assignee_name: str | None = None
    suggested_assignee_email: str | None = None
    suggested_project_name: str | None = None
    suggested_team_name: str | None = None
    confidence: str = "medium"

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, value: str) -> str:
        if value not in CONFIDENCE_VALUES:
            raise ValueError("Invalid confidence value.")
        return value


class TaskSuggestionRead(BaseModel):
    id: int
    source_type: str
    source_id: int
    title: str
    description: str | None
    suggested_start_date: date | None
    suggested_due_date: date | None
    suggested_assignee_name: str | None
    suggested_assignee_email: str | None
    suggested_project_name: str | None
    suggested_team_name: str | None
    confidence: str
    status: str
    approved_task_id: int | None
    created_at: datetime

    model_config = {
        "from_attributes": True,
    }


class ApproveTaskSuggestionRequest(BaseModel):
    project_id: int
    team_id: int
    assignee_id: int
    start_date: date
    due_date: date
    status: str = "todo"


class EmailAnalysisResult(BaseModel):
    should_create_tasks: bool
    reason: str
    source_category: str
    tasks: list[ExtractedTask]

    @field_validator("source_category")
    @classmethod
    def validate_source_category(cls, value: str) -> str:
        allowed = {
            "work_action",
            "meeting_followup",
            "client_request",
            "internal_update",
            "newsletter",
            "promotion",
            "advertisement",
            "spam",
            "receipt",
            "notification",
            "personal",
            "unknown",
        }

        if value not in allowed:
            return "unknown"

        return value