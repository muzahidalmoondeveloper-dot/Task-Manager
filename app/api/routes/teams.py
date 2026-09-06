from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.activity_actions import ENTITY_TEAM, TEAM_CREATED, TEAM_DELETED, TEAM_UPDATED
from app.core.auth_errors import AppException, ErrorDef
from app.core.database import get_db
from app.core.org_roles import ORG_MANAGEMENT_ROLES, TEAM_MEMBER
from app.core.team_access import require_team_access
from app.core.tenant import TenantContext, check_active_billing, get_tenant_context, require_org_admin, require_org_manager
from app.models.team import Team
from app.repositories.team_repository import TeamRepository
from app.repositories.user_repository import UserRepository
from app.schemas.team import (
    TeamAssignableMember,
    TeamAssignableUsersBulkRequest,
    TeamAssignableUsersBulkResponse,
    TeamCreate,
    TeamDetailRead,
    TeamRead,
    TeamUpdate,
)
from app.schemas.user import UserRead
from app.services import activity_service

router = APIRouter(prefix="/teams", tags=["Teams"])

_TEAM_NOT_FOUND = ErrorDef(code="TEAM_NOT_FOUND", status=status.HTTP_404_NOT_FOUND, message="Team not found.")
_INVALID_MANAGER = ErrorDef(code="INVALID_TEAM_MANAGER", status=status.HTTP_400_BAD_REQUEST, message="Selected team manager is not a valid org member.")
_PLAN_LIMIT_TEAMS = ErrorDef(code="PLAN_LIMIT_EXCEEDED", status=status.HTTP_402_PAYMENT_REQUIRED, message="Your plan's team limit has been reached. Please upgrade.")

# require_team_access() now lives in app.core.team_access — the single
# shared source every route touching team-scoped data (this file, Rocks,
# Issues, KPIs, Team News, ...) consults, so the rule can't drift or be
# forgotten per-route (same pattern as app.core.project_access).


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
    # Team access is scoped the same way project access is (see
    # app.core.project_access's rules for the project side of this same
    # decision): only Owner/Admin see every team. A Project Manager (base
    # role or granted flag) no longer sees every team read-only — they see
    # only team(s) they actually manage (team_manager_id) OR have been
    # separately given membership on (the same "given access to a specific
    # team" grant _require_team_access() checks below) — same rule a Team
    # Manager gets, just via whichever of the two mechanisms applies.
    repo = TeamRepository(tenant.db, tenant.organization_id)
    if tenant.is_admin_or_owner:
        teams = await repo.list_all()
    elif tenant.org_role == TEAM_MEMBER:
        teams = await repo.list_for_member(tenant.user.id)
    else:
        managed = await repo.list_for_manager(tenant.user.id)
        member_of = await repo.list_for_member(tenant.user.id)
        by_id = {t.id: t for t in managed}
        for t in member_of:
            by_id.setdefault(t.id, t)
        teams = list(by_id.values())
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
    await activity_service.record(
        db, organization_id=tenant.organization_id, actor=tenant.user,
        action=TEAM_CREATED, entity_type=ENTITY_TEAM, entity_id=team.id, entity_label=team.name,
    )
    return _serialize(team)


@router.get("/{team_id}", response_model=TeamDetailRead)
async def get_team(team_id: int, tenant: TenantContext = Depends(get_tenant_context)):
    repo = TeamRepository(tenant.db, tenant.organization_id)
    team = await repo.get_by_id(team_id)
    if team is None:
        raise AppException(_TEAM_NOT_FOUND)
    await require_team_access(tenant, repo, team_id)
    return _serialize(team)


@router.get("/{team_id}/assignable-users", response_model=list[TeamAssignableMember])
async def list_team_assignable_users(team_id: int, tenant: TenantContext = Depends(get_tenant_context)):
    """The eligible Task-assignee set for this exact team (Task Assignee
    bug-fix follow-up) — ACTIVE members only, Client always excluded
    (Rule A/B), never a member of a different team, never the full
    organization. This is the data source Team-scoped Task/To-Do
    Assignee dropdowns must use instead of the org-wide `GET /users`
    (admin-only — a Team Manager legitimately gets 403 from that one, see
    require_org_admin) or `GET /teams/{team_id}`'s own `members` field
    (which reports the legacy, non-authoritative `User.role` column —
    see list_assignable_members()'s docstring for why that can't be
    trusted to detect a Client).

    Authorization is the exact same `require_team_access` rule as
    `GET /teams/{team_id}` itself: Owner/Admin see any team; anyone else
    (including a Team Manager) only a team they manage or are a member
    of — a Team Manager never needs, and is never granted, org-wide Users
    access to get this list for their own managed team."""
    repo = TeamRepository(tenant.db, tenant.organization_id)
    team = await repo.get_by_id(team_id)
    if team is None:
        raise AppException(_TEAM_NOT_FOUND)
    await require_team_access(tenant, repo, team_id)
    members = await repo.list_assignable_members(team_id)
    return [TeamAssignableMember.model_validate(m) for m in members]


@router.post("/assignable-users/bulk", response_model=TeamAssignableUsersBulkResponse)
async def list_teams_assignable_users_bulk(
    payload: TeamAssignableUsersBulkRequest,
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Inline-assignee-dropdown bug-fix follow-up: the bulk counterpart to
    `GET /teams/{team_id}/assignable-users` — resolves eligible-assignee
    options for however many DISTINCT teams a Task list's currently
    visible rows belong to in ONE request, so a list of N tasks across M
    teams costs a small constant number of queries, never N (one per
    row) or even M (one per unique team) HTTP round-trips.

    Authorization mirrors the single-team endpoint exactly, just computed
    in bulk: Owner/Admin get every requested team_id that actually exists
    in their organization; anyone else (including a Team Manager) only
    the team_ids they manage or are a member of — a Team Manager never
    needs, and is never granted, org-wide Users access to populate this
    for their own managed team's tasks. A requested team_id that doesn't
    exist, belongs to another organization, or isn't one this caller has
    access to is simply absent from `teams` — never a 403 for the whole
    batch, and never leaks *why* it's absent."""
    team_ids = list(dict.fromkeys(payload.team_ids))  # de-dupe, preserve order
    if not team_ids:
        return TeamAssignableUsersBulkResponse(teams={})

    repo = TeamRepository(tenant.db, tenant.organization_id)
    if tenant.is_admin_or_owner:
        allowed_team_ids = await repo.filter_existing_team_ids(team_ids)
    else:
        allowed_team_ids = await repo.filter_accessible_team_ids(team_ids, tenant.user.id)

    members_by_team = await repo.list_assignable_members_bulk(list(allowed_team_ids))
    return TeamAssignableUsersBulkResponse(
        teams={
            str(team_id): [TeamAssignableMember.model_validate(u) for u in members_by_team.get(team_id, [])]
            for team_id in allowed_team_ids
        }
    )


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
    await require_team_access(tenant, repo, team_id)

    before_name = team.name
    old_member_ids = {m.user_id for m in team.memberships}
    updated = await repo.update(team, payload)

    fields_changed = []
    metadata: dict = {}
    if updated.name != before_name:
        fields_changed.append("name")
    if payload.member_ids is not None:
        new_member_ids = set(payload.member_ids) | {updated.team_manager_id}
        added = len(new_member_ids - old_member_ids)
        removed = len(old_member_ids - new_member_ids)
        if added or removed:
            # Consolidated here rather than one team.member_added/removed
            # row per user — this bulk "replace the whole roster" flow
            # doesn't have a natural single "who was added" actor-facing
            # story beyond the count; per-member events would need a
            # dedicated add/remove endpoint, which doesn't exist in this
            # app (membership is only ever changed via this bulk payload).
            metadata["members_added"], metadata["members_removed"] = added, removed
            fields_changed.append("members")
    if fields_changed:
        metadata["fields_changed"] = fields_changed
        await activity_service.record(
            db, organization_id=tenant.organization_id, actor=tenant.user,
            action=TEAM_UPDATED, entity_type=ENTITY_TEAM, entity_id=updated.id, entity_label=updated.name,
            metadata=metadata,
        )
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
    await require_team_access(tenant, repo, team_id)
    team_id_value, team_name = team.id, team.name
    await repo.delete(team)
    await activity_service.record(
        db, organization_id=tenant.organization_id, actor=tenant.user,
        action=TEAM_DELETED, entity_type=ENTITY_TEAM, entity_id=team_id_value, entity_label=team_name,
    )
    return None
