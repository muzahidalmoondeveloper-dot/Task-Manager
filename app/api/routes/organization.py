from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.core.tenant import TenantContext, get_tenant_context, require_org_admin
from app.models.objective import Objective
from app.models.org_role import OrgRole
from app.models.org_value import OrgValue
from app.models.project import Project
from app.models.rock import Rock
from app.models.user import User
from app.schemas.org import (
    ObjectiveCreate,
    ObjectiveRead,
    ObjectiveUpdate,
    OrgRoleCreate,
    OrgRoleRead,
    OrgRoleUpdate,
    OrgValueCreate,
    OrgValueRead,
    OrgValueUpdate,
)
from app.schemas.rock import RockOut

router = APIRouter(prefix="/organization", tags=["organization"])


# ─── Core Values ──────────────────────────────────────────────────────────────

@router.get("/values", response_model=list[OrgValueRead])
async def list_values(tenant: TenantContext = Depends(get_tenant_context), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(OrgValue).where(OrgValue.organization_id == tenant.organization_id).order_by(OrgValue.sort_order, OrgValue.id))
    return result.scalars().all()


@router.post("/values", response_model=OrgValueRead, status_code=status.HTTP_201_CREATED)
async def create_value(payload: OrgValueCreate, tenant: TenantContext = Depends(require_org_admin), db: AsyncSession = Depends(get_db)):
    value = OrgValue(**payload.model_dump(), organization_id=tenant.organization_id)
    db.add(value)
    await db.commit()
    await db.refresh(value)
    return value


@router.patch("/values/{value_id}", response_model=OrgValueRead)
async def update_value(value_id: int, payload: OrgValueUpdate, tenant: TenantContext = Depends(require_org_admin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(OrgValue).where(OrgValue.id == value_id, OrgValue.organization_id == tenant.organization_id))
    value = result.scalar_one_or_none()
    if not value:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Value not found.")
    for key, val in payload.model_dump(exclude_unset=True).items():
        setattr(value, key, val)
    await db.commit()
    await db.refresh(value)
    return value


@router.delete("/values/{value_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_value(value_id: int, tenant: TenantContext = Depends(require_org_admin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(OrgValue).where(OrgValue.id == value_id, OrgValue.organization_id == tenant.organization_id))
    value = result.scalar_one_or_none()
    if not value:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Value not found.")
    await db.delete(value)
    await db.commit()


# ─── Objectives ───────────────────────────────────────────────────────────────

async def _validate_project_id(project_id: int | None, tenant: TenantContext, db: AsyncSession) -> None:
    if project_id is None:
        return
    result = await db.execute(select(Project.id).where(Project.id == project_id, Project.organization_id == tenant.organization_id))
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")


@router.get("/objectives", response_model=list[ObjectiveRead])
async def list_objectives(tenant: TenantContext = Depends(get_tenant_context), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Objective).where(Objective.organization_id == tenant.organization_id).options(selectinload(Objective.owner), selectinload(Objective.project)).order_by(Objective.created_at.desc()))
    return result.scalars().all()


@router.get("/objective-rocks", response_model=list[RockOut])
async def list_objective_rocks(tenant: TenantContext = Depends(get_tenant_context), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Rock).where(Rock.objective_id.isnot(None), Rock.organization_id == tenant.organization_id).order_by(Rock.objective_id, Rock.created_at.desc()))
    return result.scalars().all()


@router.post("/objectives", response_model=ObjectiveRead, status_code=status.HTTP_201_CREATED)
async def create_objective(payload: ObjectiveCreate, tenant: TenantContext = Depends(require_org_admin), db: AsyncSession = Depends(get_db)):
    await _validate_project_id(payload.project_id, tenant, db)
    obj = Objective(**payload.model_dump(), created_by_id=tenant.user.id, organization_id=tenant.organization_id)
    db.add(obj)
    await db.commit()
    result = await db.execute(select(Objective).where(Objective.id == obj.id).options(selectinload(Objective.owner), selectinload(Objective.project)))
    return result.scalar_one()


@router.patch("/objectives/{obj_id}", response_model=ObjectiveRead)
async def update_objective(obj_id: int, payload: ObjectiveUpdate, tenant: TenantContext = Depends(require_org_admin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Objective).where(Objective.id == obj_id, Objective.organization_id == tenant.organization_id).options(selectinload(Objective.owner)))
    obj = result.scalar_one_or_none()
    if not obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Objective not found.")
    updates = payload.model_dump(exclude_unset=True)
    if "project_id" in updates:
        await _validate_project_id(updates["project_id"], tenant, db)
    for key, val in updates.items():
        setattr(obj, key, val)
    await db.commit()
    result2 = await db.execute(select(Objective).where(Objective.id == obj_id).options(selectinload(Objective.owner), selectinload(Objective.project)))
    return result2.scalar_one()


@router.delete("/objectives/{obj_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_objective(obj_id: int, tenant: TenantContext = Depends(require_org_admin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Objective).where(Objective.id == obj_id, Objective.organization_id == tenant.organization_id))
    obj = result.scalar_one_or_none()
    if not obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Objective not found.")
    await db.delete(obj)
    await db.commit()


# ─── Org Roles ────────────────────────────────────────────────────────────────

@router.get("/roles", response_model=list[OrgRoleRead])
async def list_roles(tenant: TenantContext = Depends(get_tenant_context), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(OrgRole).where(OrgRole.organization_id == tenant.organization_id).options(selectinload(OrgRole.assignees)).order_by(OrgRole.created_at.asc()))
    return result.scalars().all()


@router.post("/roles", response_model=OrgRoleRead, status_code=status.HTTP_201_CREATED)
async def create_role(payload: OrgRoleCreate, tenant: TenantContext = Depends(require_org_admin), db: AsyncSession = Depends(get_db)):
    data = payload.model_dump()
    assignee_ids = data.pop("assignee_ids", [])
    role = OrgRole(**data, organization_id=tenant.organization_id)
    if assignee_ids:
        users_result = await db.execute(select(User).where(User.id.in_(assignee_ids)))
        role.assignees = list(users_result.scalars().all())
    else:
        role.assignees = []
    db.add(role)
    await db.commit()
    result = await db.execute(select(OrgRole).where(OrgRole.id == role.id).options(selectinload(OrgRole.assignees)))
    return result.scalar_one()


@router.patch("/roles/{role_id}", response_model=OrgRoleRead)
async def update_role(role_id: int, payload: OrgRoleUpdate, tenant: TenantContext = Depends(require_org_admin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(OrgRole).where(OrgRole.id == role_id, OrgRole.organization_id == tenant.organization_id).options(selectinload(OrgRole.assignees)))
    role = result.scalar_one_or_none()
    if not role:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found.")
    data = payload.model_dump(exclude_unset=True)
    assignee_ids = data.pop("assignee_ids", None)
    for key, val in data.items():
        setattr(role, key, val)
    if assignee_ids is not None:
        users_result = await db.execute(select(User).where(User.id.in_(assignee_ids)))
        role.assignees = list(users_result.scalars().all())
    await db.commit()
    result2 = await db.execute(select(OrgRole).where(OrgRole.id == role_id).options(selectinload(OrgRole.assignees)))
    return result2.scalar_one()


@router.delete("/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role(role_id: int, tenant: TenantContext = Depends(require_org_admin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(OrgRole).where(OrgRole.id == role_id, OrgRole.organization_id == tenant.organization_id))
    role = result.scalar_one_or_none()
    if not role:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found.")
    await db.delete(role)
    await db.commit()
