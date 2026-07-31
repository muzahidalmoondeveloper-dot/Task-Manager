from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.tenant import TenantContext, get_tenant_context
from app.models.issue import Issue, IssueLink
from app.repositories.project_repository import ProjectRepository
from app.schemas.issue import EntityLinkIn, IssueCreate, IssueUpdate, IssueOut

_ISSUE_CREATE_FORBIDDEN = HTTPException(
    status_code=status.HTTP_403_FORBIDDEN,
    detail="You can only create issues under a project you are assigned to.",
)


def _apply_links(issue: Issue, links: list[EntityLinkIn]) -> None:
    issue.links = [
        IssueLink(linked_type=l.linked_type, linked_id=l.linked_id, title=l.title)
        for l in links
    ]

router = APIRouter(prefix="/teams/{team_id}/issues", tags=["issues"])


@router.get("", response_model=list[IssueOut])
async def list_issues(
    team_id: int,
    timeframe: str = Query(None),
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    q = select(Issue).where(
        Issue.team_id == team_id,
        Issue.organization_id == tenant.organization_id,
    )
    if timeframe:
        q = q.where(Issue.timeframe == timeframe)
    q = q.order_by(Issue.created_at.desc())
    result = await db.execute(q)
    return result.scalars().all()


@router.post("", response_model=IssueOut, status_code=status.HTTP_201_CREATED)
async def create_issue(
    team_id: int,
    payload: IssueCreate,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    if not tenant.is_manager_or_above:
        if not tenant.has_project_manager_access:
            raise _ISSUE_CREATE_FORBIDDEN
        project_repo = ProjectRepository(db, tenant.organization_id)
        if payload.project_id is None or not await project_repo.is_member(payload.project_id, tenant.user.id):
            raise _ISSUE_CREATE_FORBIDDEN

    issue = Issue(team_id=team_id, organization_id=tenant.organization_id, **payload.model_dump(exclude={"links"}))
    _apply_links(issue, payload.links)
    db.add(issue)
    await db.commit()
    await db.refresh(issue)
    return issue


@router.patch("/{issue_id}", response_model=IssueOut)
async def update_issue(
    team_id: int,
    issue_id: int,
    payload: IssueUpdate,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    result = await db.execute(
        select(Issue).where(
            Issue.id == issue_id,
            Issue.team_id == team_id,
            Issue.organization_id == tenant.organization_id,
        )
    )
    issue = result.scalar_one_or_none()
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")

    if payload.status == "resolved" and issue.status != "resolved":
        issue.resolved_at = datetime.now(timezone.utc)
    elif payload.status is not None and payload.status != "resolved" and issue.status == "resolved":
        issue.resolved_at = None

    for field, value in payload.model_dump(exclude_none=True, exclude={"links"}).items():
        setattr(issue, field, value)
    if payload.links is not None:
        for l in list(issue.links):
            await db.delete(l)
        await db.flush()
        _apply_links(issue, payload.links)
    await db.commit()
    await db.refresh(issue)
    return issue


@router.delete("/{issue_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_issue(
    team_id: int,
    issue_id: int,
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_tenant_context),
):
    result = await db.execute(
        select(Issue).where(
            Issue.id == issue_id,
            Issue.team_id == team_id,
            Issue.organization_id == tenant.organization_id,
        )
    )
    issue = result.scalar_one_or_none()
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")
    await db.delete(issue)
    await db.commit()
