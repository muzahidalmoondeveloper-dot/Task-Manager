"""Bulk create/replace helpers for the report snapshot tables, plus the
report-version duplication mechanism (spec §29 — editing a finalized report
creates a new version rather than mutating it)."""

from __future__ import annotations

import uuid

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.report import (
    Report,
    ReportContent,
    ReportIssueSnapshot,
    ReportKpiSnapshot,
    ReportMilestoneSnapshot,
    ReportRiskSnapshot,
    ReportRockSnapshot,
    ReportTaskSnapshot,
    ReportVersion,
)


class ReportSnapshotService:
    def __init__(self, db: AsyncSession, org_id: uuid.UUID) -> None:
        self.db = db
        self.org_id = org_id

    async def clear_snapshots(self, report_id: int) -> None:
        """Delete all existing snapshot rows for a report (draft regenerate)."""
        for model in (
            ReportRockSnapshot,
            ReportKpiSnapshot,
            ReportMilestoneSnapshot,
            ReportTaskSnapshot,
            ReportRiskSnapshot,
            ReportIssueSnapshot,
        ):
            await self.db.execute(delete(model).where(model.report_id == report_id))

    async def replace_rock_snapshots(self, report_id: int, rows: list[dict]) -> None:
        for i, row in enumerate(rows):
            self.db.add(ReportRockSnapshot(report_id=report_id, organization_id=self.org_id, sort_order=i, **row))

    async def replace_kpi_snapshots(self, report_id: int, rows: list[dict]) -> None:
        for row in rows:
            self.db.add(ReportKpiSnapshot(report_id=report_id, organization_id=self.org_id, **row))

    async def replace_milestone_snapshots(self, report_id: int, rows: list[dict]) -> None:
        for row in rows:
            self.db.add(ReportMilestoneSnapshot(report_id=report_id, organization_id=self.org_id, **row))

    async def replace_task_snapshots(self, report_id: int, rows: list[dict]) -> None:
        for row in rows:
            self.db.add(ReportTaskSnapshot(report_id=report_id, organization_id=self.org_id, **row))

    async def replace_risk_snapshots(self, report_id: int, rows: list[dict]) -> None:
        for row in rows:
            self.db.add(ReportRiskSnapshot(report_id=report_id, organization_id=self.org_id, **row))

    async def replace_issue_snapshots(self, report_id: int, rows: list[dict]) -> None:
        for row in rows:
            self.db.add(ReportIssueSnapshot(report_id=report_id, organization_id=self.org_id, **row))

    async def duplicate_as_new_version(self, source: Report, created_by_id: int | None) -> Report:
        """Clone a finalized report into a new draft, one version higher.

        The source report is left untouched (immutable history); the clone
        becomes the new ``is_latest_version`` row in the ``parent_report_id``
        chain, per spec §29.
        """
        new_report = Report(
            organization_id=self.org_id,
            project_id=source.project_id,
            report_type=source.report_type,
            title=source.title,
            period_start=source.period_start,
            period_end=source.period_end,
            status="draft",
            version=source.version + 1,
            parent_report_id=source.id,
            is_latest_version=True,
            theme_id=source.theme_id,
            client_branding_id=source.client_branding_id,
            created_by_id=created_by_id,
            team_visible=source.team_visible,
        )
        self.db.add(new_report)
        await self.db.flush()

        if source.content is not None:
            self.db.add(ReportContent(
                report_id=new_report.id,
                executive_summary=source.content.executive_summary,
                key_achievement=source.content.key_achievement,
                current_challenge=source.content.current_challenge,
                next_priority=source.content.next_priority,
                client_attention=source.content.client_attention,
                upcoming_plan_notes=source.content.upcoming_plan_notes,
                final_remarks=source.content.final_remarks,
            ))

        def _copy_fields(snapshot, exclude: set[str]) -> dict:
            return {
                c.name: getattr(snapshot, c.name)
                for c in snapshot.__table__.columns
                if c.name not in exclude
            }

        _exclude = {"id", "report_id", "organization_id", "created_at"}
        for snap in source.rock_snapshots:
            self.db.add(ReportRockSnapshot(report_id=new_report.id, organization_id=self.org_id, **_copy_fields(snap, _exclude)))
        for snap in source.kpi_snapshots:
            self.db.add(ReportKpiSnapshot(report_id=new_report.id, organization_id=self.org_id, **_copy_fields(snap, _exclude)))
        for snap in source.milestone_snapshots:
            self.db.add(ReportMilestoneSnapshot(report_id=new_report.id, organization_id=self.org_id, **_copy_fields(snap, _exclude)))
        for snap in source.task_snapshots:
            self.db.add(ReportTaskSnapshot(report_id=new_report.id, organization_id=self.org_id, **_copy_fields(snap, _exclude)))
        for snap in source.risk_snapshots:
            self.db.add(ReportRiskSnapshot(report_id=new_report.id, organization_id=self.org_id, **_copy_fields(snap, _exclude)))
        for snap in source.issue_snapshots:
            self.db.add(ReportIssueSnapshot(report_id=new_report.id, organization_id=self.org_id, **_copy_fields(snap, _exclude)))

        source.is_latest_version = False
        self.db.add(ReportVersion(root_report_id=source.parent_report_id or source.id, report_id=new_report.id, version_number=new_report.version))

        await self.db.commit()
        await self.db.refresh(new_report)
        return new_report
