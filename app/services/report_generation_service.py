"""Builds a Report's snapshot rows from a project's live data.

Called on report create and on "regenerate data" (draft only) — never on a
finalized report, whose snapshots must stay frozen (spec §31).
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.issue import Issue
from app.models.kpi import KPI
from app.models.report import Report
from app.models.risk import Risk
from app.models.rock import Rock
from app.models.task import Task
from app.services import report_calculation_service as calc
from app.services.report_snapshot_service import ReportSnapshotService


def _user_name(user) -> str | None:
    if user is None:
        return None
    return getattr(user, "full_name", None) or getattr(user, "email", None)


class ReportGenerationService:
    def __init__(self, db: AsyncSession, org_id: uuid.UUID) -> None:
        self.db = db
        self.org_id = org_id
        self.snapshots = ReportSnapshotService(db, org_id)

    async def _load_rocks(self, project_id: int) -> list[Rock]:
        stmt = (
            select(Rock)
            .where(Rock.organization_id == self.org_id, Rock.project_id == project_id, Rock.is_archived.is_(False))
            .options(selectinload(Rock.milestones), selectinload(Rock.owner))
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def _load_kpis(self, project_id: int) -> list[KPI]:
        stmt = (
            select(KPI)
            .where(KPI.organization_id == self.org_id, KPI.project_id == project_id)
            .options(selectinload(KPI.entries))
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def _load_tasks(self, project_id: int) -> list[Task]:
        stmt = (
            select(Task)
            .where(Task.organization_id == self.org_id, Task.project_id == project_id)
            .options(selectinload(Task.assignee))
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def _load_risks(self, project_id: int) -> list[Risk]:
        stmt = (
            select(Risk)
            .where(Risk.organization_id == self.org_id, Risk.project_id == project_id)
            .options(selectinload(Risk.owner))
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def _load_issues(self, project_id: int) -> list[Issue]:
        stmt = (
            select(Issue)
            .where(Issue.organization_id == self.org_id, Issue.project_id == project_id)
            .options(selectinload(Issue.assignee))
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def generate(self, report: Report) -> None:
        """Refresh every snapshot table for ``report`` from current project data."""
        if report.project_id is None:
            return

        rocks = await self._load_rocks(report.project_id)
        kpis = await self._load_kpis(report.project_id)
        tasks = await self._load_tasks(report.project_id)
        risks = await self._load_risks(report.project_id)
        issues = await self._load_issues(report.project_id)
        milestones = [m for r in rocks for m in (r.milestones or [])]

        await self.snapshots.clear_snapshots(report.id)

        await self.snapshots.replace_rock_snapshots(report.id, [
            {
                "rock_id": rock.id,
                "title": rock.title,
                "status": rock.status,
                "owner_name": _user_name(rock.owner),
                "due_date": rock.due_date,
                "progress_pct": calc.rock_progress_pct(rock),
            }
            for rock in rocks
        ])

        await self.snapshots.replace_kpi_snapshots(report.id, [
            {
                "kpi_id": kpi.id,
                "name": kpi.title,
                "target_value": kpi.reference_value,
                "actual_value": calc.kpi_latest_value(kpi),
                "previous_value": calc.kpi_previous_value(kpi),
                "trend": calc.kpi_trend(kpi),
                "status": calc.kpi_status(kpi),
            }
            for kpi in kpis
        ])

        await self.snapshots.replace_milestone_snapshots(report.id, [
            {
                "milestone_id": m.id,
                "rock_id": m.rock_id,
                "title": m.title,
                "status": m.status,
                "planned_start_date": m.planned_start_date,
                "planned_end_date": m.planned_end_date or m.due_date,
                "actual_start_date": m.actual_start_date,
                "actual_end_date": m.actual_end_date,
                "forecast_end_date": m.forecast_end_date,
                "delay_days": calc.milestone_delay_days(m),
            }
            for m in milestones
        ])

        await self.snapshots.replace_task_snapshots(report.id, [
            {
                "task_id": task.id,
                "name": task.name,
                "status": task.status,
                "assignee_name": _user_name(task.assignee),
                "due_date": task.due_date,
                "is_overdue": bool(
                    task.due_date and task.status not in calc.TASK_DONE_STATUSES
                    and task.due_date < date.today()
                ),
                "is_blocked": task.status in calc.TASK_BLOCKED_STATUSES,
            }
            for task in tasks
        ])

        await self.snapshots.replace_risk_snapshots(report.id, [
            {
                "risk_id": risk.id,
                "title": risk.title,
                "description": risk.description,
                "severity": risk.severity,
                "likelihood": risk.likelihood,
                "owner_name": _user_name(risk.owner),
                "mitigation_plan": risk.mitigation_plan,
                "status": risk.status,
            }
            for risk in risks
        ])

        await self.snapshots.replace_issue_snapshots(report.id, [
            {
                "issue_id": issue.id,
                "title": issue.title,
                "description": issue.description,
                "status": issue.status,
                "owner_name": _user_name(issue.assignee),
                "resolution_plan": issue.resolution_plan,
                "target_resolution_date": issue.target_resolution_date,
                "resolved_at": issue.resolved_at,
            }
            for issue in issues
        ])

        await self.db.commit()

    async def calculate_dashboard(self, report: Report) -> dict:
        """Read-only summary numbers for the Executive Dashboard, computed
        from the report's *own* snapshots (immutable once finalized) rather
        than live data."""
        return calc.dashboard_summary(report)
