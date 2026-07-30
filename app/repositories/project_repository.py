import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.project import Project, ProjectMembership
from app.repositories.base_tenant_repository import TenantRepository
from app.schemas.project import ProjectCreate, ProjectUpdate


class ProjectRepository(TenantRepository):
    def __init__(self, db: AsyncSession, org_id: uuid.UUID) -> None:
        super().__init__(db, org_id)

    async def list_all(self) -> list[Project]:
        stmt = (
            select(Project)
            .where(Project.organization_id == self.org_id)
            .order_by(Project.created_at.desc())
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def get_by_id(self, project_id: int) -> Project | None:
        stmt = select(Project).where(
            Project.id == project_id,
            Project.organization_id == self.org_id,
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def create(self, payload: ProjectCreate, created_by_id: int) -> Project:
        project = Project(
            name=payload.name.strip(),
            description=payload.description,
            status=payload.status,
            created_by_id=created_by_id,
            organization_id=self.org_id,
        )
        self.db.add(project)
        await self.db.commit()
        await self.db.refresh(project)
        return project

    async def update(self, project: Project, payload: ProjectUpdate) -> Project:
        data = payload.model_dump(exclude_unset=True)
        if "name" in data and data["name"]:
            data["name"] = data["name"].strip()
        for key, value in data.items():
            setattr(project, key, value)
        await self.db.commit()
        await self.db.refresh(project)
        return project

    async def delete(self, project: Project) -> None:
        await self.db.delete(project)
        await self.db.commit()

    # ── Project Manager assignment (ProjectMembership) ──────────────────────

    async def list_for_user(self, user_id: int) -> list[Project]:
        stmt = (
            select(Project)
            .join(ProjectMembership, ProjectMembership.project_id == Project.id)
            .where(
                Project.organization_id == self.org_id,
                ProjectMembership.user_id == user_id,
            )
            .order_by(Project.created_at.desc())
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def is_member(self, project_id: int, user_id: int) -> bool:
        stmt = select(ProjectMembership.id).where(
            ProjectMembership.project_id == project_id,
            ProjectMembership.user_id == user_id,
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def list_members(self, project_id: int) -> list[ProjectMembership]:
        stmt = (
            select(ProjectMembership)
            .where(ProjectMembership.project_id == project_id)
            .options(selectinload(ProjectMembership.user))
            .order_by(ProjectMembership.created_at.asc())
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def add_member(self, project_id: int, user_id: int) -> ProjectMembership:
        existing = await self.db.execute(
            select(ProjectMembership).where(
                ProjectMembership.project_id == project_id,
                ProjectMembership.user_id == user_id,
            )
        )
        membership = existing.scalar_one_or_none()
        if membership is not None:
            return membership

        membership = ProjectMembership(project_id=project_id, user_id=user_id)
        self.db.add(membership)
        await self.db.commit()
        await self.db.refresh(membership, attribute_names=["user"])
        return membership

    async def remove_member(self, project_id: int, user_id: int) -> None:
        stmt = select(ProjectMembership).where(
            ProjectMembership.project_id == project_id,
            ProjectMembership.user_id == user_id,
        )
        result = await self.db.execute(stmt)
        membership = result.scalar_one_or_none()
        if membership is not None:
            await self.db.delete(membership)
            await self.db.commit()
