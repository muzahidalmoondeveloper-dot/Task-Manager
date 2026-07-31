from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_errors import AppException, ErrorDef
from app.core.database import get_db
from app.core.org_roles import ORG_MANAGEMENT_ROLES, PROJECT_MANAGER, TEAM_MEMBER
from app.core.tenant import TenantContext, check_active_billing, get_tenant_context, require_org_admin, require_org_manager
from app.models.team import Team
from app.repositories.team_repository import TeamRepository
from app.repositories.user_repository import UserRepository
from app.schemas.team import TeamCreate, TeamDetailRead, TeamRead, TeamUpdate
from app.schemas.user import UserRead

router = APIRouter(prefix="/teams", tags=["Teams"])

_TEAM_NOT_FOUND = ErrorDef(code="TEAM_NOT_FOUND", status=status.HTTP_404_NOT_FOUND, message="Team not found.")
_INVALID_MANAGER = ErrorDef(code="INVALID_TEAM_MANAGER", status=status.HTTP_400_BAD_REQUEST, message="Selected team manager is not a valid org member.")
_PLAN_LIMIT_TEAMS = ErrorDef(code="PLAN_LIMIT_EXCEEDED", status=status.HTTP_402_PAYMENT_REQUIRED, message="Your plan's team limit has been reached. Please upgrade.")


def _serialize(team: Team) -> TeamDetailRead:
    return TeamDetailRead(
        id=team.id,
        name=team.name,
        description=team.description,
        team_manager_id=team.team_manager_id,
        created_by_id=team.created_by_id,
        created_at=team.created_at,
        updated_at=team.updated_at,
        team_manager=UserRead.model_validate(team.team_manager),
        members=[
            UserRead.model_validate(m.user) for m in team.memberships if m.user is not None
        ],
    )


@router.get("", response_model=list[TeamDetailRead])
async def list_teams(tenant: TenantContext = Depends(get_tenant_context)):
    # Project managers are scoped to their assigned project(s) for project
    # visibility (see projects.py's _PROJECT_SCOPED_ROLES), but per their role
    # definition they can still view every team read-only — e.g. to assign a
    # team when creating an issue or task under their project.
    repo = TeamRepository(tenant.db, tenant.organization_id)
    if tenant.is_admin_or_owner or tenant.org_role == PROJECT_MANAGER:
        teams = await repo.list_all()
    elif tenant.org_role == TEAM_MEMBER:
        teams = await repo.list_for_member(tenant.user.id)
    else:
        teams = await repo.list_for_manager(tenant.user.id)
    return [_serialize(t) for t in teams]


@router.post("", response_model=TeamDetailRead, status_code=status.HTTP_201_CREATED)
async def create_team(
    payload: TeamCreate,
    tenant: TenantContext = Depends(require_org_manager),
    db: AsyncSession = Depends(get_db),
):
    check_active_billing(tenant)
    limits = tenant.plan_limits
    if limits.max_teams != -1:
        repo_check = TeamRepository(db, tenant.organization_id)
        teams = await repo_check.list_all()
        if len(teams) >= limits.max_teams:
            raise AppException(_PLAN_LIMIT_TEAMS, details={"limit": limits.max_teams, "resource": "teams"})

    user_repo = UserRepository(db)
    manager = await user_repo.get_by_id(payload.team_manager_id)
    if manager is None:
        raise AppException(_INVALID_MANAGER)

    for member_id in payload.member_ids:
        member = await user_repo.get_by_id(member_id)
        if member is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid member id: {member_id}")

    repo = TeamRepository(db, tenant.organization_id)
    team = await repo.create(payload, created_by_id=tenant.user.id)
    return _serialize(team)


@router.get("/{team_id}", response_model=TeamDetailRead)
async def get_team(team_id: int, tenant: TenantContext = Depends(get_tenant_context)):
    repo = TeamRepository(tenant.db, tenant.organization_id)
    team = await repo.get_by_id(team_id)
    if team is None:
        raise AppException(_TEAM_NOT_FOUND)
    return _serialize(team)


@router.patch("/{team_id}", response_model=TeamDetailRead)
async def update_team(
    team_id: int,
    payload: TeamUpdate,
    tenant: TenantContext = Depends(require_org_manager),
    db: AsyncSession = Depends(get_db),
):
    repo = TeamRepository(db, tenant.organization_id)
    team = await repo.get_by_id(team_id)
    if team is None:
        raise AppException(_TEAM_NOT_FOUND)
    updated = await repo.update(team, payload)
    return _serialize(updated)


@router.delete("/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_team(
    team_id: int,
    tenant: TenantContext = Depends(require_org_manager),
    db: AsyncSession = Depends(get_db),
):
    repo = TeamRepository(db, tenant.organization_id)
    team = await repo.get_by_id(team_id)
    if team is None:
        raise AppException(_TEAM_NOT_FOUND)
    await repo.delete(team)
    return None
