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


class TeamAssignableMember(BaseModel):
    """One eligible Task-assignee for this team — Task Assignee bug-fix
    follow-up (GET /teams/{team_id}/assignable-users). Deliberately NOT
    UserRead: this is a public-within-the-org identity summary for a
    dropdown, not the fuller user record — no email, no
    verification/OTP timestamps, no org-admin/manager flags. Only
    ACTIVE, non-Client members of this exact team are ever included
    (never the full org, never a member of a different team) — see
    app.repositories.team_repository.TeamRepository.list_assignable_members
    for the actual eligibility query."""

    id: int
    full_name: str
    profile_picture_url: str | None = None

    model_config = {"from_attributes": True}


class TeamAssignableUsersBulkRequest(BaseModel):
    """Inline-assignee-dropdown bug-fix follow-up: lets a Task list/table
    view (TasksPage, ProjectDetailPage, ...) resolve assignable-member
    options for however many DISTINCT teams its currently-visible Team
    Tasks belong to — ONE request, never one `GET /teams/{id}/
    assignable-users` per task row. Bounded so a client can't force an
    unbounded per-team aggregation with an arbitrarily large id list."""

    team_ids: list[int] = Field(default_factory=list, max_length=200)


class TeamAssignableUsersBulkResponse(BaseModel):
    # Keyed by team id as a string (JSON object keys are always strings) —
    # only team_ids the caller is actually authorized to see one for; a
    # requested-but-not-visible/nonexistent/other-org team_id is silently
    # omitted, never an error (matches the equivalent bulk Task Working
    # Time endpoint's own "partial visibility" convention).
    teams: dict[str, list[TeamAssignableMember]]