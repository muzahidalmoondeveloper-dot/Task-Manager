from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.team_access import require_team_access
from app.core.tenant import TenantContext, get_tenant_context
from app.models.team_news import TeamNews, TeamNewsLink
from app.repositories.team_repository import TeamRepository
from app.schemas.team_news import NewsCreate, NewsLinkIn, NewsOut, NewsUpdate

router = APIRouter(prefix="/teams/{team_id}/news", tags=["team-news"])


async def _validate_team(db: AsyncSession, tenant: TenantContext, team_id: int) -> None:
    """Raise 404 if team_id doesn't belong to the caller's org, then 403 if
    the caller isn't assigned to it (see app.core.team_access)."""
    repo = TeamRepository(db, tenant.organization_id)
    team = await repo.get_by_id(team_id)
    if team is None:
        raise HTTPException(status_code=404, detail="Team not found")
    await require_team_access(tenant, repo, team_id)


def _apply_links(news: TeamNews, links: list[NewsLinkIn]) -> None:
    news.links = [
        TeamNewsLink(linked_type=link.linked_type, linked_id=link.linked_id, title=link.title)
        for link in links
    ]


@router.get("", response_model=list[NewsOut])
async def list_news(
    team_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    await require_team_access(tenant, TeamRepository(db, tenant.organization_id), team_id)
    result = await db.execute(
        select(TeamNews)
        .where(
            TeamNews.team_id == team_id,
            TeamNews.organization_id == tenant.organization_id,
        )
        .order_by(TeamNews.created_at.desc())
    )
    return result.scalars().unique().all()


@router.post("", response_model=NewsOut, status_code=status.HTTP_201_CREATED)
async def create_news(
    team_id: int,
    payload: NewsCreate,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    await _validate_team(db, tenant, team_id)

    data = payload.model_dump(exclude={"links"})
    news = TeamNews(team_id=team_id, organization_id=tenant.organization_id, **data)
    _apply_links(news, payload.links)
    db.add(news)
    await db.commit()
    await db.refresh(news)
    return news


@router.patch("/{news_id}", response_model=NewsOut)
async def update_news(
    team_id: int,
    news_id: int,
    payload: NewsUpdate,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    await require_team_access(tenant, TeamRepository(db, tenant.organization_id), team_id)
    result = await db.execute(
        select(TeamNews).where(
            TeamNews.id == news_id,
            TeamNews.team_id == team_id,
            TeamNews.organization_id == tenant.organization_id,
        )
    )
    news = result.scalar_one_or_none()
    if not news:
        raise HTTPException(status_code=404, detail="News not found")

    data = payload.model_dump(exclude_unset=True, exclude={"links"})
    new_team_id = data.pop("team_id", None)
    if new_team_id is not None:
        await _validate_team(db, tenant, new_team_id)
        news.team_id = new_team_id

    for key, value in data.items():
        setattr(news, key, value)

    if payload.links is not None:
        for l in list(news.links):
            await db.delete(l)
        await db.flush()
        _apply_links(news, payload.links)

    await db.commit()
    await db.refresh(news)
    return news


@router.delete("/{news_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_news(
    team_id: int,
    news_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    await require_team_access(tenant, TeamRepository(db, tenant.organization_id), team_id)
    result = await db.execute(
        select(TeamNews).where(
            TeamNews.id == news_id,
            TeamNews.team_id == team_id,
            TeamNews.organization_id == tenant.organization_id,
        )
    )
    news = result.scalar_one_or_none()
    if not news:
        raise HTTPException(status_code=404, detail="News not found")
    await db.delete(news)
    await db.commit()
