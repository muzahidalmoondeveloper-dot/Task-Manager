from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.tenant import TenantContext, get_tenant_context
from app.models.rock import Milestone, Rock, RockLink
from app.schemas.rock import EntityLinkIn, RockCreate, RockOut, RockUpdate


def _apply_links(rock: Rock, links: list[EntityLinkIn]) -> None:
    rock.links = [
        RockLink(linked_type=l.linked_type, linked_id=l.linked_id, title=l.title)
        for l in links
    ]

router = APIRouter(prefix="/teams/{team_id}/rocks", tags=["rocks"])


@router.get("", response_model=list[RockOut])
async def list_rocks(
    team_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    result = await db.execute(
        select(Rock)
        .where(Rock.team_id == team_id, Rock.organization_id == tenant.organization_id)
        .order_by(Rock.created_at.desc())
    )
    return result.scalars().all()


@router.post("", response_model=RockOut, status_code=status.HTTP_201_CREATED)
async def create_rock(
    team_id: int,
    payload: RockCreate,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    milestones_data = payload.milestones or []
    rock_data = payload.model_dump(exclude={"milestones", "links"})
    rock = Rock(team_id=team_id, organization_id=tenant.organization_id, **rock_data)
    for i, m in enumerate(milestones_data):
        milestone = Milestone(sort_order=i, **m.model_dump(exclude={"id", "sort_order"}))
        rock.milestones.append(milestone)
    _apply_links(rock, payload.links)
    db.add(rock)
    await db.commit()
    await db.refresh(rock)
    return rock


@router.patch("/{rock_id}", response_model=RockOut)
async def update_rock(
    team_id: int,
    rock_id: int,
    payload: RockUpdate,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    result = await db.execute(
        select(Rock).where(
            Rock.id == rock_id,
            Rock.team_id == team_id,
            Rock.organization_id == tenant.organization_id,
        )
    )
    rock = result.scalar_one_or_none()
    if not rock:
        raise HTTPException(status_code=404, detail="Rock not found")

    for key, value in payload.model_dump(exclude_unset=True, exclude={"milestones", "links"}).items():
        setattr(rock, key, value)

    if payload.milestones is not None:
        for m in list(rock.milestones):
            await db.delete(m)
        await db.flush()
        for i, m_data in enumerate(payload.milestones):
            milestone = Milestone(rock_id=rock_id, sort_order=i, **m_data.model_dump(exclude={"id", "sort_order"}))
            db.add(milestone)

    if payload.links is not None:
        for l in list(rock.links):
            await db.delete(l)
        await db.flush()
        _apply_links(rock, payload.links)

    await db.commit()
    await db.refresh(rock)
    return rock


@router.delete("/{rock_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rock(
    team_id: int,
    rock_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    result = await db.execute(
        select(Rock).where(
            Rock.id == rock_id,
            Rock.team_id == team_id,
            Rock.organization_id == tenant.organization_id,
        )
    )
    rock = result.scalar_one_or_none()
    if not rock:
        raise HTTPException(status_code=404, detail="Rock not found")
    await db.delete(rock)
    await db.commit()
