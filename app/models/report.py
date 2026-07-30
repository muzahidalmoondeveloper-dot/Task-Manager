import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.core.database import Base


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    organization_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True)
    project_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True)

    report_type: Mapped[str] = mapped_column(String(20), nullable=False, default="monthly", server_default="monthly")
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    period_start: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    period_end: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft", server_default="draft")

    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    parent_report_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("reports.id", ondelete="SET NULL"), nullable=True)
    is_latest_version: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")

    theme_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("report_themes.id", ondelete="SET NULL"), nullable=True)
    client_branding_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("client_brandings.id", ondelete="SET NULL"), nullable=True)

    created_by_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    finalized_by_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    finalized_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    pdf_file_path: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    team_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    # Employee/Team Performance reports — nullable, unused by project reports.
    employee_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    team_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("teams.id", ondelete="SET NULL"), nullable=True)
    performance_snapshot: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    project = relationship("Project", foreign_keys=[project_id], lazy="selectin")
    employee = relationship("User", foreign_keys=[employee_id], lazy="selectin")
    team = relationship("Team", foreign_keys=[team_id], lazy="selectin")
    created_by = relationship("User", foreign_keys=[created_by_id], lazy="selectin")
    finalized_by = relationship("User", foreign_keys=[finalized_by_id], lazy="selectin")
    theme = relationship("ReportTheme", foreign_keys=[theme_id], lazy="selectin")
    branding = relationship("ClientBranding", foreign_keys=[client_branding_id], lazy="selectin")

    content: Mapped[Optional["ReportContent"]] = relationship(
        "ReportContent", back_populates="report", uselist=False, lazy="selectin", cascade="all, delete-orphan",
    )
    rock_snapshots: Mapped[list["ReportRockSnapshot"]] = relationship(
        "ReportRockSnapshot", back_populates="report", lazy="selectin", cascade="all, delete-orphan",
        order_by="ReportRockSnapshot.sort_order",
    )
    kpi_snapshots: Mapped[list["ReportKpiSnapshot"]] = relationship(
        "ReportKpiSnapshot", back_populates="report", lazy="selectin", cascade="all, delete-orphan",
    )
    milestone_snapshots: Mapped[list["ReportMilestoneSnapshot"]] = relationship(
        "ReportMilestoneSnapshot", back_populates="report", lazy="selectin", cascade="all, delete-orphan",
    )
    task_snapshots: Mapped[list["ReportTaskSnapshot"]] = relationship(
        "ReportTaskSnapshot", back_populates="report", lazy="selectin", cascade="all, delete-orphan",
    )
    risk_snapshots: Mapped[list["ReportRiskSnapshot"]] = relationship(
        "ReportRiskSnapshot", back_populates="report", lazy="selectin", cascade="all, delete-orphan",
    )
    issue_snapshots: Mapped[list["ReportIssueSnapshot"]] = relationship(
        "ReportIssueSnapshot", back_populates="report", lazy="selectin", cascade="all, delete-orphan",
    )
    client_actions: Mapped[list["ReportClientAction"]] = relationship(
        "ReportClientAction", back_populates="report", lazy="selectin", cascade="all, delete-orphan",
        order_by="ReportClientAction.sort_order",
    )


class ReportContent(Base):
    __tablename__ = "report_content"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    report_id: Mapped[int] = mapped_column(Integer, ForeignKey("reports.id", ondelete="CASCADE"), nullable=False, unique=True)

    executive_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    key_achievement: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    current_challenge: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    next_priority: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    client_attention: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    upcoming_plan_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    final_remarks: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    report: Mapped["Report"] = relationship("Report", back_populates="content")


class ReportTheme(Base):
    __tablename__ = "report_themes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    organization_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    is_preset: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    primary_color: Mapped[str] = mapped_column(String(7), nullable=False, default="#1E3A8A", server_default="#1E3A8A")
    accent_color: Mapped[str] = mapped_column(String(7), nullable=False, default="#3B82F6", server_default="#3B82F6")
    secondary_color: Mapped[str] = mapped_column(String(7), nullable=False, default="#0F172A", server_default="#0F172A")
    font_family: Mapped[str] = mapped_column(String(100), nullable=False, default="Inter", server_default="Inter")
    logo_url: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ClientBranding(Base):
    __tablename__ = "client_brandings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    organization_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True)
    project_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True)

    client_name: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    client_logo_url: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    company_logo_url: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    signature_image_url: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    signature_name: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    signature_title: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ReportRockSnapshot(Base):
    __tablename__ = "report_rock_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    report_id: Mapped[int] = mapped_column(Integer, ForeignKey("reports.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    rock_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("rocks.id", ondelete="SET NULL"), nullable=True)

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="backlog", server_default="backlog")
    owner_name: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    due_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    progress_pct: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    commentary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    report: Mapped["Report"] = relationship("Report", back_populates="rock_snapshots")


class ReportKpiSnapshot(Base):
    __tablename__ = "report_kpi_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    report_id: Mapped[int] = mapped_column(Integer, ForeignKey("reports.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    kpi_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("kpis.id", ondelete="SET NULL"), nullable=True)

    name: Mapped[str] = mapped_column(String(300), nullable=False)
    target_value: Mapped[Optional[float]] = mapped_column(nullable=True)
    actual_value: Mapped[Optional[float]] = mapped_column(nullable=True)
    previous_value: Mapped[Optional[float]] = mapped_column(nullable=True)
    trend: Mapped[str] = mapped_column(String(20), nullable=False, default="stable", server_default="stable")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="on_track", server_default="on_track")
    commentary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    report: Mapped["Report"] = relationship("Report", back_populates="kpi_snapshots")


class ReportMilestoneSnapshot(Base):
    __tablename__ = "report_milestone_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    report_id: Mapped[int] = mapped_column(Integer, ForeignKey("reports.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    milestone_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("milestones.id", ondelete="SET NULL"), nullable=True)
    rock_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("rocks.id", ondelete="SET NULL"), nullable=True)

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", server_default="pending")
    planned_start_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    planned_end_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    actual_start_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    actual_end_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    forecast_end_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    delay_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    report: Mapped["Report"] = relationship("Report", back_populates="milestone_snapshots")


class ReportTaskSnapshot(Base):
    __tablename__ = "report_task_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    report_id: Mapped[int] = mapped_column(Integer, ForeignKey("reports.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    task_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)

    name: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="todo", server_default="todo")
    assignee_name: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    due_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    is_overdue: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    is_blocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    blocker: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    report: Mapped["Report"] = relationship("Report", back_populates="task_snapshots")


class ReportRiskSnapshot(Base):
    __tablename__ = "report_risk_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    report_id: Mapped[int] = mapped_column(Integer, ForeignKey("reports.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    risk_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("risks.id", ondelete="SET NULL"), nullable=True)

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, default="medium", server_default="medium")
    likelihood: Mapped[str] = mapped_column(String(20), nullable=False, default="medium", server_default="medium")
    owner_name: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    mitigation_plan: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open", server_default="open")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    report: Mapped["Report"] = relationship("Report", back_populates="risk_snapshots")


class ReportIssueSnapshot(Base):
    __tablename__ = "report_issue_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    report_id: Mapped[int] = mapped_column(Integer, ForeignKey("reports.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    issue_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("issues.id", ondelete="SET NULL"), nullable=True)

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open", server_default="open")
    owner_name: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    resolution_plan: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    target_resolution_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    report: Mapped["Report"] = relationship("Report", back_populates="issue_snapshots")


class ReportClientAction(Base):
    __tablename__ = "report_client_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    report_id: Mapped[int] = mapped_column(Integer, ForeignKey("reports.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    responsible_person: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    owner_user_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    due_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    priority: Mapped[str] = mapped_column(String(20), nullable=False, default="medium", server_default="medium")
    impact_if_delayed: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", server_default="pending")
    admin_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    report: Mapped["Report"] = relationship("Report", back_populates="client_actions")


class ReportVersion(Base):
    __tablename__ = "report_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    root_report_id: Mapped[int] = mapped_column(Integer, ForeignKey("reports.id", ondelete="CASCADE"), nullable=False, index=True)
    report_id: Mapped[int] = mapped_column(Integer, ForeignKey("reports.id", ondelete="CASCADE"), nullable=False, unique=True)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
