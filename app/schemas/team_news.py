from datetime import datetime

from pydantic import BaseModel, field_validator

LINKABLE_TYPES = {"objective", "rock", "task", "kpi"}


class UserRef(BaseModel):
    id: int
    full_name: str | None = None
    email: str
    model_config = {"from_attributes": True}


class NewsLinkIn(BaseModel):
    linked_type: str
    linked_id: int
    title: str

    @field_validator("linked_type")
    @classmethod
    def validate_linked_type(cls, v: str) -> str:
        if v not in LINKABLE_TYPES:
            raise ValueError(f"linked_type must be one of: {', '.join(sorted(LINKABLE_TYPES))}")
        return v


class NewsLinkOut(BaseModel):
    linked_type: str
    linked_id: int
    title: str
    model_config = {"from_attributes": True}


class NewsCreate(BaseModel):
    title: str
    body: str | None = None
    icon: str | None = None
    status: str = "active"
    owner_id: int | None = None
    links: list[NewsLinkIn] = []


class NewsUpdate(BaseModel):
    title: str | None = None
    body: str | None = None
    icon: str | None = None
    status: str | None = None
    owner_id: int | None = None
    team_id: int | None = None
    # None = leave links untouched; [] or a list = replace the full set
    links: list[NewsLinkIn] | None = None


class NewsOut(BaseModel):
    id: int
    title: str
    body: str | None = None
    icon: str | None = None
    status: str
    team_id: int
    owner_id: int | None = None
    owner: UserRef | None = None
    links: list[NewsLinkOut] = []
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}
