from datetime import date, datetime
from typing import Optional, List

from pydantic import BaseModel, field_validator

LINKABLE_TYPES = {"objective", "rock", "task", "kpi"}


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


class UserRef(BaseModel):
    id: int
    full_name: Optional[str] = None
    email: str
    model_config = {"from_attributes": True}


class ObjectiveRef(BaseModel):
    id: int
    title: str
    model_config = {"from_attributes": True}


class ProjectRef(BaseModel):
    id: int
    name: str
    model_config = {"from_attributes": True}


class MilestoneUpsert(BaseModel):
    id: Optional[int] = None
    title: str
    description: Optional[str] = None
    status: str = "pending"
    due_date: Optional[date] = None
    owner_id: Optional[int] = None
    sort_order: int = 0


class MilestoneOut(BaseModel):
    id: int
    title: str
    description: Optional[str] = None
    status: str
    due_date: Optional[date] = None
    sort_order: int
    owner_id: Optional[int] = None
    owner: Optional[UserRef] = None
    model_config = {"from_attributes": True}


class RockCreate(BaseModel):
    title: str
    icon: Optional[str] = None
    description: Optional[str] = None
    status: str = "backlog"
    owner_id: Optional[int] = None
    objective_id: Optional[int] = None
    project_id: Optional[int] = None
    due_date: Optional[date] = None
    tags: Optional[list] = None
    milestones: Optional[List[MilestoneUpsert]] = None
    links: List[EntityLinkIn] = []


class RockUpdate(BaseModel):
    title: Optional[str] = None
    icon: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    owner_id: Optional[int] = None
    objective_id: Optional[int] = None
    project_id: Optional[int] = None
    due_date: Optional[date] = None
    tags: Optional[list] = None
    milestones: Optional[List[MilestoneUpsert]] = None
    is_archived: Optional[bool] = None
    team_id: Optional[int] = None
    links: Optional[List[EntityLinkIn]] = None


class RockOut(BaseModel):
    id: int
    title: str
    icon: Optional[str] = None
    description: Optional[str] = None
    status: str
    is_archived: bool = False
    due_date: Optional[date] = None
    tags: Optional[list] = None
    team_id: int
    owner_id: Optional[int] = None
    objective_id: Optional[int] = None
    project_id: Optional[int] = None
    owner: Optional[UserRef] = None
    objective: Optional[ObjectiveRef] = None
    project: Optional[ProjectRef] = None
    milestones: List[MilestoneOut] = []
    links: List[EntityLinkOut] = []
    created_at: datetime
    model_config = {"from_attributes": True}
