from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, field_validator

from app.core.report_constants import REPORT_TYPES


class ReportProjectRef(BaseModel):
    id: int
    name: str
    model_config = {"from_attributes": True}


class ReportUserRef(BaseModel):
    id: int
    full_name: Optional[str] = None
    email: str
    model_config = {"from_attributes": True}


class ReportThemeOut(BaseModel):
    id: int
    name: str
    is_preset: bool
    primary_color: str
    accent_color: str
    secondary_color: str
    font_family: str
    logo_url: Optional[str] = None
    model_config = {"from_attributes": True}


class ReportThemeCreate(BaseModel):
    name: str
    primary_color: str = "#1E3A8A"
    accent_color: str = "#3B82F6"
    secondary_color: str = "#0F172A"
    font_family: str = "Inter"
    logo_url: Optional[str] = None


class ReportThemeUpdate(BaseModel):
    name: Optional[str] = None
    primary_color: Optional[str] = None
    accent_color: Optional[str] = None
    secondary_color: Optional[str] = None
    font_family: Optional[str] = None
    logo_url: Optional[str] = None


class ClientBrandingOut(BaseModel):
    id: int
    project_id: Optional[int] = None
    client_name: Optional[str] = None
    client_logo_url: Optional[str] = None
    company_logo_url: Optional[str] = None
    signature_image_url: Optional[str] = None
    signature_name: Optional[str] = None
    signature_title: Optional[str] = None
    model_config = {"from_attributes": True}


class ClientBrandingUpsert(BaseModel):
    project_id: Optional[int] = None
    client_name: Optional[str] = None
    client_logo_url: Optional[str] = None
    company_logo_url: Optional[str] = None
    signature_image_url: Optional[str] = None
    signature_name: Optional[str] = None
    signature_title: Optional[str] = None


class ReportCreate(BaseModel):
    project_id: int
    report_type: str = "monthly"
    title: str
    period_start: Optional[date] = None
    period_end: Optional[date] = None

    @field_validator("report_type")
    @classmethod
    def validate_report_type(cls, value: str) -> str:
        if value not in REPORT_TYPES:
            raise ValueError(f"report_type must be one of: {', '.join(sorted(REPORT_TYPES))}")
        return value


class EmployeeReportCreate(BaseModel):
    employee_id: int
    period: str = "this_month"
    project_id: Optional[int] = None
    team_id: Optional[int] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    include_task_details: bool = False


class TeamReportCreate(BaseModel):
    team_id: int
    period: str = "this_month"
    project_id: Optional[int] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    include_task_details: bool = False


class ReportContentUpdate(BaseModel):
    executive_summary: Optional[str] = None
    key_achievement: Optional[str] = None
    current_challenge: Optional[str] = None
    next_priority: Optional[str] = None
    client_attention: Optional[str] = None
    upcoming_plan_notes: Optional[str] = None
    final_remarks: Optional[str] = None


class ReportUpdate(BaseModel):
    title: Optional[str] = None
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    theme_id: Optional[int] = None
    client_branding_id: Optional[int] = None
    team_visible: Optional[bool] = None
    content: Optional[ReportContentUpdate] = None


class ReportContentOut(BaseModel):
    executive_summary: Optional[str] = None
    key_achievement: Optional[str] = None
    current_challenge: Optional[str] = None
    next_priority: Optional[str] = None
    client_attention: Optional[str] = None
    upcoming_plan_notes: Optional[str] = None
    final_remarks: Optional[str] = None
    model_config = {"from_attributes": True}


class ReportRockSnapshotOut(BaseModel):
    id: int
    rock_id: Optional[int] = None
    title: str
    status: str
    owner_name: Optional[str] = None
    due_date: Optional[date] = None
    progress_pct: int
    commentary: Optional[str] = None
    model_config = {"from_attributes": True}


class ReportKpiSnapshotOut(BaseModel):
    id: int
    kpi_id: Optional[int] = None
    name: str
    target_value: Optional[float] = None
    actual_value: Optional[float] = None
    previous_value: Optional[float] = None
    trend: str
    status: str
    commentary: Optional[str] = None
    model_config = {"from_attributes": True}


class ReportMilestoneSnapshotOut(BaseModel):
    id: int
    milestone_id: Optional[int] = None
    rock_id: Optional[int] = None
    title: str
    status: str
    planned_start_date: Optional[date] = None
    planned_end_date: Optional[date] = None
    actual_start_date: Optional[date] = None
    actual_end_date: Optional[date] = None
    forecast_end_date: Optional[date] = None
    delay_days: Optional[int] = None
    notes: Optional[str] = None
    model_config = {"from_attributes": True}


class ReportTaskSnapshotOut(BaseModel):
    id: int
    task_id: Optional[int] = None
    name: str
    status: str
    assignee_name: Optional[str] = None
    due_date: Optional[date] = None
    is_overdue: bool
    is_blocked: bool
    blocker: Optional[str] = None
    model_config = {"from_attributes": True}


class ReportRiskSnapshotOut(BaseModel):
    id: int
    risk_id: Optional[int] = None
    title: str
    description: Optional[str] = None
    severity: str
    likelihood: str
    owner_name: Optional[str] = None
    mitigation_plan: Optional[str] = None
    status: str
    model_config = {"from_attributes": True}


class ReportIssueSnapshotOut(BaseModel):
    id: int
    issue_id: Optional[int] = None
    title: str
    description: Optional[str] = None
    status: str
    owner_name: Optional[str] = None
    resolution_plan: Optional[str] = None
    target_resolution_date: Optional[date] = None
    resolved_at: Optional[datetime] = None
    model_config = {"from_attributes": True}


class ReportClientActionOut(BaseModel):
    id: int
    title: str
    description: Optional[str] = None
    responsible_person: Optional[str] = None
    owner_user_id: Optional[int] = None
    due_date: Optional[date] = None
    priority: str
    impact_if_delayed: Optional[str] = None
    status: str
    admin_note: Optional[str] = None
    model_config = {"from_attributes": True}


class ReportClientActionUpsert(BaseModel):
    id: Optional[int] = None
    title: str
    description: Optional[str] = None
    responsible_person: Optional[str] = None
    owner_user_id: Optional[int] = None
    due_date: Optional[date] = None
    priority: str = "medium"
    impact_if_delayed: Optional[str] = None
    status: str = "pending"
    admin_note: Optional[str] = None


class ReportListItem(BaseModel):
    id: int
    project_id: Optional[int] = None
    employee_id: Optional[int] = None
    team_id: Optional[int] = None
    report_type: str
    title: str
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    status: str
    version: int
    is_latest_version: bool
    team_visible: bool
    project: Optional[ReportProjectRef] = None
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


class ReportDetail(ReportListItem):
    parent_report_id: Optional[int] = None
    finalized_at: Optional[datetime] = None
    pdf_file_path: Optional[str] = None
    created_by: Optional[ReportUserRef] = None
    finalized_by: Optional[ReportUserRef] = None
    theme: Optional[ReportThemeOut] = None
    branding: Optional[ClientBrandingOut] = None
    content: Optional[ReportContentOut] = None
    rock_snapshots: list[ReportRockSnapshotOut] = []
    kpi_snapshots: list[ReportKpiSnapshotOut] = []
    milestone_snapshots: list[ReportMilestoneSnapshotOut] = []
    task_snapshots: list[ReportTaskSnapshotOut] = []
    risk_snapshots: list[ReportRiskSnapshotOut] = []
    issue_snapshots: list[ReportIssueSnapshotOut] = []
    client_actions: list[ReportClientActionOut] = []
