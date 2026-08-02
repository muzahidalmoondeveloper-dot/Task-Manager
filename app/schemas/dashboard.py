from pydantic import BaseModel

from app.schemas.project import ProjectRead
from app.schemas.task import TaskDetailRead
from app.schemas.team import TeamRead


class OrgTotals(BaseModel):
    users: int
    teams: int
    projects: int


class DashboardSummaryRead(BaseModel):
    # "admin" (org-wide), "manager" (team-manager and/or project-manager
    # scoped — a user can be both, in which case tasks are the union), or
    # "team_member" (assigned-to-me only).
    role_view: str
    tasks: list[TaskDetailRead]
    # For "admin" this is every team/project in the org; for "manager" it's
    # only the ones this user manages / is assigned to; empty for "team_member".
    teams: list[TeamRead] = []
    projects: list[ProjectRead] = []
    org_totals: OrgTotals | None = None

    model_config = {"from_attributes": True}
