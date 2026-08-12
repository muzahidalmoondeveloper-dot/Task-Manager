from datetime import datetime

from pydantic import BaseModel, Field, field_validator


PROJECT_STATUSES = {"active", "paused", "completed", "cancelled"}


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    status: str = "active"

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in PROJECT_STATUSES:
            raise ValueError("Invalid project status.")
        return value


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    status: str | None = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str | None) -> str | None:
        if value is not None and value not in PROJECT_STATUSES:
            raise ValueError("Invalid project status.")
        return value


class ProjectRead(BaseModel):
    id: int
    name: str
    description: str | None
    logo_url: str | None = None
    status: str
    created_by_id: int
    created_at: datetime
    updated_at: datetime
    project_manager_id: int | None = None
    project_manager_name: str | None = None

    model_config = {
        "from_attributes": True,
    }


class ProjectMemberAssign(BaseModel):
    user_id: int


class ProjectMemberOut(BaseModel):
    id: int
    user_id: int
    full_name: str | None = None
    email: str

    model_config = {
        "from_attributes": True,
    }