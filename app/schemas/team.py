from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.user import UserRead


class TeamCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    team_manager_id: int
    member_ids: list[int] = []


class TeamUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    team_manager_id: int | None = None
    member_ids: list[int] | None = None


class TeamRead(BaseModel):
    id: int
    name: str
    description: str | None
    team_manager_id: int
    created_by_id: int
    created_at: datetime
    updated_at: datetime

    model_config = {
        "from_attributes": True,
    }


class TeamDetailRead(TeamRead):
    team_manager: UserRead
    members: list[UserRead]