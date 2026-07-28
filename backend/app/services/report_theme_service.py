"""Preset theme seeding for the Report Module (spec §23)."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.report_constants import THEME_PRESETS
from app.models.report import ReportTheme
from app.repositories.report_repository import ReportRepository


class ReportThemeService:
    def __init__(self, db: AsyncSession, org_id: uuid.UUID) -> None:
        self.db = db
        self.org_id = org_id
        self.repo = ReportRepository(db, org_id)

    async def list_themes_with_presets(self) -> list[ReportTheme]:
        """Returns the org's saved themes, seeding the standard presets the
        first time this org accesses the Report Module's Branding tab."""
        existing = await self.repo.list_themes()
        if existing:
            return existing

        for preset in THEME_PRESETS:
            self.db.add(ReportTheme(organization_id=self.org_id, is_preset=True, **preset))
        await self.db.commit()
        return await self.repo.list_themes()
