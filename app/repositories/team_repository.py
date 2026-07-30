import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.team import Team, TeamMembership
from app.models.task import Task
from app.repositories.base_tenant_repository import TenantRepository
from app.schemas.team import TeamCreate, TeamUpdate


class TeamRepository(TenantRepository):
    def __init__(self, db: AsyncSession, org_id: uuid.UUID) -> None:
        super().__init__(db, org_id)

    def _base_query(self):
        return (
            select(Team)
            .where(Team.organization_id == self.org_id)
            .options(
                selectinload(Team.team_manager),
                selectinload(Team.memberships).selectinload(TeamMembership.user),
            )
        )

    async def list_all(self) -> list[Team]:
        stmt = self._base_query().order_by(Team.created_at.desc())
        result = await self.db.execute(stmt)
        return list(result.scalars().unique().all())

    async def list_for_manager(self, manager_id: int) -> list[Team]:
        stmt = (
            self._base_query()
            .where(Team.team_manager_id == manager_id)
            .order_by(Team.name.asc())
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().unique().all())

    async def list_for_member(self, user_id: int) -> list[Team]:
        stmt = (
            self._base_query()
            .join(TeamMembership, TeamMembership.team_id == Team.id)
            .where(TeamMembership.user_id == user_id)
            .order_by(Team.name.asc())
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().unique().all())

    async def get_by_id(self, team_id: int) -> Team | None:
        stmt = self._base_query().where(Team.id == team_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def create(self, payload: TeamCreate, created_by_id: int) -> Team:
        team = Team(
            name=payload.name.strip(),
            description=payload.description,
            team_manager_id=payload.team_manager_id,
            created_by_id=created_by_id,
            organization_id=self.org_id,
        )
        self.db.add(team)
        await self.db.flush()

        member_ids = set(payload.member_ids)
        member_ids.add(payload.team_manager_id)

        for user_id in member_ids:
            self.db.add(TeamMembership(team_id=team.id, user_id=user_id))

        await self.db.commit()
        return await self.get_by_id(team.id)

    async def update(self, team: Team, payload: TeamUpdate) -> Team:
        data = payload.model_dump(exclude_unset=True)
        member_ids = data.pop("member_ids", None)

        for key, value in data.items():
            setattr(team, key, value)

        if member_ids is not None:
            await self.db.execute(
                delete(TeamMembership).where(TeamMembership.team_id == team.id)
            )
            final_ids = set(member_ids)
            if team.team_manager_id:
                final_ids.add(team.team_manager_id)
            for user_id in final_ids:
                self.db.add(TeamMembership(team_id=team.id, user_id=user_id))

        await self.db.commit()
        return await self.get_by_id(team.id)

    async def delete(self, team: Team) -> None:
        await self.db.delete(team)
        await self.db.commit()

    async def list_by_project(self, project_id: int) -> list[Task]:
        stmt = (
            select(Task)
            .where(Task.project_id == project_id, Task.organization_id == self.org_id)
            .options(selectinload(Task.assignee), selectinload(Task.project))
            .order_by(Task.due_date.asc(), Task.created_at.desc())
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())
