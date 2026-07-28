import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.risk import Risk
from app.repositories.base_tenant_repository import TenantRepository
from app.schemas.risk import RiskCreate, RiskUpdate

_RISK_EAGER = [
    selectinload(Risk.owner),
    selectinload(Risk.project),
]


class RiskRepository(TenantRepository):
    def __init__(self, db: AsyncSession, org_id: uuid.UUID) -> None:
        super().__init__(db, org_id)

    def _base_stmt(self):
        return select(Risk).where(Risk.organization_id == self.org_id).options(*_RISK_EAGER)

    async def list_by_team(self, team_id: int, *, status: str | None = None, project_id: int | None = None) -> list[Risk]:
        stmt = self._base_stmt().where(Risk.team_id == team_id)
        if status:
            stmt = stmt.where(Risk.status == status)
        if project_id:
            stmt = stmt.where(Risk.project_id == project_id)
        stmt = stmt.order_by(Risk.created_at.desc())
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def list_by_project(self, project_id: int) -> list[Risk]:
        stmt = self._base_stmt().where(Risk.project_id == project_id).order_by(Risk.created_at.desc())
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def get_by_id(self, risk_id: int) -> Risk | None:
        stmt = self._base_stmt().where(Risk.id == risk_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def create(self, team_id: int, payload: RiskCreate) -> Risk:
        risk = Risk(
            team_id=team_id,
            organization_id=self.org_id,
            **payload.model_dump(),
        )
        self.db.add(risk)
        await self.db.commit()
        return await self.get_by_id(risk.id)

    async def update(self, risk: Risk, payload: RiskUpdate) -> Risk:
        data = payload.model_dump(exclude_unset=True)
        for key, value in data.items():
            setattr(risk, key, value)
        await self.db.commit()
        return await self.get_by_id(risk.id)

    async def delete(self, risk: Risk) -> None:
        await self.db.delete(risk)
        await self.db.commit()
