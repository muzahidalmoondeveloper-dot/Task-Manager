from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, field_validator

LINKABLE_TYPES = {"objective", "rock", "task", "kpi"}
ISSUE_STATUSES = {"open", "in_progress", "resolved", "closed"}


class EntityLinkIn(BaseModel):
    linked_type: str
    linked_id: int
    title: str

    @field_validator("linked_type")
    @classmethod
    def validate_linked_type(cls, v: str) -> str:
        if v not in LINKABLE_TYPES:
            raise ValueError(f"linked_type must be one of: {', '.join(sorted(LINKABLE_TYPES))}")
        return v


class EntityLinkOut(BaseModel):
    linked_type: str
    linked_id: int
    title: str
    model_config = {"from_attributes": True}


class AssigneeRef(BaseModel):
    id: int
    full_name: Optional[str] = None
    email: str

    model_config = {"from_attributes": True}


class ProjectRef(BaseModel):
    id: int
    name: str

    model_config = {"from_attributes": True}


class IssueCreate(BaseModel):
    title: str
    description: Optional[str] = None
    icon: Optional[str] = None
    assignee_id: Optional[int] = None
    project_id: Optional[int] = None
    timeframe: Optional[str] = "short-term"
    priority: Optional[int] = 0
    status: Optional[str] = "open"
    resolution_plan: Optional[str] = None
    target_resolution_date: Optional[date] = None
    links: list[EntityLinkIn] = []

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in ISSUE_STATUSES:
            raise ValueError(f"status must be one of: {', '.join(sorted(ISSUE_STATUSES))}")
        return value


class IssueUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    icon: Optional[str] = None
    assignee_id: Optional[int] = None
    project_id: Optional[int] = None
    timeframe: Optional[str] = None
    priority: Optional[int] = None
    team_id: Optional[int] = None
    status: Optional[str] = None
    resolution_plan: Optional[str] = None
    target_resolution_date: Optional[date] = None
    resolved_at: Optional[datetime] = None
    links: Optional[list[EntityLinkIn]] = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in ISSUE_STATUSES:
            raise ValueError(f"status must be one of: {', '.join(sorted(ISSUE_STATUSES))}")
        return value


class IssueOut(BaseModel):
    id: int
    title: str
    description: Optional[str] = None
    icon: Optional[str] = None
    team_id: int
    assignee_id: Optional[int] = None
    project_id: Optional[int] = None
    timeframe: str
    priority: int
    status: str
    resolution_plan: Optional[str] = None
    target_resolution_date: Optional[date] = None
    resolved_at: Optional[datetime] = None
    assignee: Optional[AssigneeRef] = None
    project: Optional[ProjectRef] = None
    links: list[EntityLinkOut] = []
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
