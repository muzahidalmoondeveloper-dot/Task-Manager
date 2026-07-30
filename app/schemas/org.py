from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


# ─── Core Values ──────────────────────────────────────────────────────────────

class OrgValueCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str | None = None
    icon: str = "⭐"
    color: str = "slate"
    sort_order: int = 0


class OrgValueUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    icon: str | None = None
    color: str | None = None
    sort_order: int | None = None


class OrgValueRead(BaseModel):
    id: int
    title: str
    description: str | None
    icon: str
    color: str
    sort_order: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ─── Objectives ───────────────────────────────────────────────────────────────

class ObjectiveCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str | None = None
    icon: str | None = None
    status: str = "active"
    progress: int = Field(default=0, ge=0, le=100)
    due_date: date | None = None
    owner_id: int | None = None
    project_id: int | None = None


class ObjectiveUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    icon: str | None = None
    status: str | None = None
    progress: int | None = Field(default=None, ge=0, le=100)
    due_date: date | None = None
    owner_id: int | None = None
    project_id: int | None = None


class ObjectiveOwnerRead(BaseModel):
    id: int
    full_name: str
    email: str
    model_config = {"from_attributes": True}


class ObjectiveProjectRead(BaseModel):
    id: int
    name: str
    model_config = {"from_attributes": True}


class ObjectiveRead(BaseModel):
    id: int
    title: str
    description: str | None
    icon: str | None = None
    status: str
    progress: int
    due_date: date | None
    owner_id: int | None
    project_id: int | None
    created_by_id: int | None
    created_at: datetime
    updated_at: datetime
    owner: ObjectiveOwnerRead | None = None
    project: ObjectiveProjectRead | None = None

    model_config = {"from_attributes": True}


# ─── Org Roles ────────────────────────────────────────────────────────────────

class ResponsibilityItem(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str | None = None


class OrgRoleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    responsibilities: list[ResponsibilityItem] = []
    parent_id: int | None = None
    assignee_ids: list[int] = []


class OrgRoleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    responsibilities: list[ResponsibilityItem] | None = None
    parent_id: int | None = None
    assignee_ids: list[int] | None = None


class OrgRoleUserRead(BaseModel):
    id: int
    full_name: str | None
    email: str
    model_config = ConfigDict(from_attributes=True)


class OrgRoleRead(BaseModel):
    id: int
    name: str
    responsibilities: list[ResponsibilityItem]
    parent_id: int | None
    assignees: list[OrgRoleUserRead]
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)
