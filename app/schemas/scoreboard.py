from datetime import date, datetime

from pydantic import BaseModel


class ScoreboardSummary(BaseModel):
    total_assigned: int
    total_completed: int
    completed_before_due: int
    completed_on_due: int
    completed_after_due: int
    completed_no_due_date: int
    overdue: int
    pending: int
    completion_rate: float
    on_time_rate: float


class ScoreBreakdown(BaseModel):
    has_data: bool
    completion_score: float
    on_time_score: float
    overdue_score: float
    total_score: float
    rounded_score: int
    performance_level: str
    change_from_previous: int | None = None


class ScoreHistoryPoint(BaseModel):
    period_label: str
    period_start: date
    period_end: date
    rounded_score: int | None = None
    completed_tasks: int | None = None
    overdue_tasks: int | None = None
    has_data: bool


class ScoreboardTaskItem(BaseModel):
    id: int
    name: str
    project_id: int | None = None
    project_name: str | None = None
    priority: str
    due_date: date | None = None
    completed_at: datetime | None = None
    status: str
    score_impact: str


class ScoreboardEmployee(BaseModel):
    id: int
    full_name: str
    email: str
    role: str
    teams: list[str] = []

    model_config = {"from_attributes": True}


class ScoreboardResponse(BaseModel):
    employee: ScoreboardEmployee
    period: str
    period_start: date
    period_end: date
    summary: ScoreboardSummary
    score: ScoreBreakdown
    explanation: list[str]
    trend: list[ScoreHistoryPoint]


class TeamInfo(BaseModel):
    id: int
    name: str
    description: str | None = None
    manager_name: str | None = None
    member_count: int


class TeamScoreboardMemberRow(BaseModel):
    rank: int
    user_id: int
    full_name: str
    role: str
    has_data: bool
    rounded_score: int | None = None
    performance_level: str | None = None
    total_assigned: int
    total_completed: int
    overdue: int
    completion_rate: float
    on_time_rate: float


class TeamScoreboardResponse(BaseModel):
    team: TeamInfo
    period: str
    period_start: date
    period_end: date
    summary: ScoreboardSummary
    score: ScoreBreakdown
    members: list[TeamScoreboardMemberRow]
    trend: list[ScoreHistoryPoint]
    previous_trend: list[ScoreHistoryPoint]
    insights: list[str]


class OrgScoreboardEmployeeRow(BaseModel):
    rank: int
    user_id: int
    full_name: str
    role: str
    manager_id: int | None = None
    manager_name: str | None = None
    team_id: int | None = None
    team_name: str | None = None
    has_data: bool
    rounded_score: int | None = None
    performance_level: str | None = None
    total_completed: int
    on_time_rate: float
    overdue: int


class OrgScoreboardResponse(BaseModel):
    period: str
    period_start: date
    period_end: date
    employees: list[OrgScoreboardEmployeeRow]


class TeamRankingRow(BaseModel):
    rank: int
    team_id: int
    team_name: str
    manager_id: int | None = None
    manager_name: str | None = None
    member_count: int
    has_data: bool
    rounded_score: int | None = None
    performance_level: str | None = None
    total_completed: int
    on_time_rate: float
    overdue: int


class TeamRankingResponse(BaseModel):
    period: str
    period_start: date
    period_end: date
    teams: list[TeamRankingRow]


class ManagerRankingRow(BaseModel):
    rank: int
    manager_id: int
    manager_name: str
    team_count: int
    employee_count: int
    has_data: bool
    rounded_score: int | None = None
    performance_level: str | None = None
    total_completed: int
    on_time_rate: float
    overdue: int


class ManagerRankingResponse(BaseModel):
    period: str
    period_start: date
    period_end: date
    managers: list[ManagerRankingRow]
