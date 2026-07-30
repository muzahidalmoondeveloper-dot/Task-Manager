import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.report import ClientBranding, Report, ReportContent, ReportTheme, ReportVersion
from app.repositories.base_tenant_repository import TenantRepository
from app.schemas.report import ClientBrandingUpsert, ReportCreate, ReportThemeCreate, ReportThemeUpdate


class ReportRepository(TenantRepository):
    def __init__(self, db: AsyncSession, org_id: uuid.UUID) -> None:
        super().__init__(db, org_id)

    def _base_stmt(self):
        return select(Report).where(Report.organization_id == self.org_id)

    async def list_all(
        self,
        *,
        project_id: int | None = None,
        report_type: str | None = None,
        status: str | None = None,
        latest_only: bool = True,
    ) -> list[Report]:
        stmt = self._base_stmt()
        if project_id:
            stmt = stmt.where(Report.project_id == project_id)
        if report_type:
            stmt = stmt.where(Report.report_type == report_type)
        if status:
            stmt = stmt.where(Report.status == status)
        if latest_only:
            stmt = stmt.where(Report.is_latest_version.is_(True))
        stmt = stmt.order_by(Report.created_at.desc())
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def get_by_id(self, report_id: int) -> Report | None:
        stmt = self._base_stmt().where(Report.id == report_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def list_versions(self, root_report_id: int) -> list[ReportVersion]:
        stmt = select(ReportVersion).where(ReportVersion.root_report_id == root_report_id).order_by(ReportVersion.version_number.asc())
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def create(self, payload: ReportCreate, created_by_id: int) -> Report:
        report = Report(
            organization_id=self.org_id,
            project_id=payload.project_id,
            report_type=payload.report_type,
            title=payload.title,
            period_start=payload.period_start,
            period_end=payload.period_end,
            status="draft",
            created_by_id=created_by_id,
        )
        self.db.add(report)
        await self.db.flush()
        self.db.add(ReportContent(report_id=report.id))
        await self.db.commit()
        return await self.get_by_id(report.id)

    async def delete(self, report: Report) -> None:
        await self.db.delete(report)
        await self.db.commit()

    # ── Theme / branding ──────────────────────────────────────────────────

    async def list_themes(self) -> list[ReportTheme]:
        stmt = select(ReportTheme).where(ReportTheme.organization_id == self.org_id).order_by(ReportTheme.created_at.asc())
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def get_theme(self, theme_id: int) -> ReportTheme | None:
        stmt = select(ReportTheme).where(ReportTheme.organization_id == self.org_id, ReportTheme.id == theme_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def create_theme(self, payload: ReportThemeCreate) -> ReportTheme:
        theme = ReportTheme(organization_id=self.org_id, **payload.model_dump())
        self.db.add(theme)
        await self.db.commit()
        await self.db.refresh(theme)
        return theme

    async def update_theme(self, theme: ReportTheme, payload: ReportThemeUpdate) -> ReportTheme:
        data = payload.model_dump(exclude_unset=True)
        for key, value in data.items():
            setattr(theme, key, value)
        await self.db.commit()
        await self.db.refresh(theme)
        return theme

    async def get_branding(self, branding_id: int) -> ClientBranding | None:
        stmt = select(ClientBranding).where(ClientBranding.organization_id == self.org_id, ClientBranding.id == branding_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def list_branding(self) -> list[ClientBranding]:
        stmt = select(ClientBranding).where(ClientBranding.organization_id == self.org_id).order_by(ClientBranding.created_at.asc())
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def get_branding_for_project(self, project_id: int | None) -> ClientBranding | None:
        stmt = select(ClientBranding).where(ClientBranding.organization_id == self.org_id, ClientBranding.project_id == project_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def upsert_branding(self, payload: ClientBrandingUpsert) -> ClientBranding:
        existing = await self.get_branding_for_project(payload.project_id)
        if existing is not None:
            for key, value in payload.model_dump(exclude_unset=True).items():
                setattr(existing, key, value)
            await self.db.commit()
            await self.db.refresh(existing)
            return existing

        branding = ClientBranding(organization_id=self.org_id, **payload.model_dump())
        self.db.add(branding)
        await self.db.commit()
        await self.db.refresh(branding)
        return branding
