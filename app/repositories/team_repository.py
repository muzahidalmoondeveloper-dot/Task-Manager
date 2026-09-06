import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.org_roles import CLIENT
from app.models.organization import OrganizationMembership
from app.models.team import Team, TeamMembership
from app.models.task import Task
from app.models.user import User
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

    async def is_manager(self, team_id: int, user_id: int) -> bool:
        """True only if `user_id` is SPECIFICALLY this team's manager —
        unlike has_access() below, a plain TeamMembership row does not
        count. Used for the Team Manager Task-scope follow-up: a plain
        Team Manager gets Task read access via actually managing the
        team, never merely being a member of it. Same cheap two-column
        check as has_access(), no eager-loading."""
        stmt = select(Team.team_manager_id).where(Team.id == team_id, Team.organization_id == self.org_id)
        manager_id = (await self.db.execute(stmt)).scalar_one_or_none()
        return manager_id is not None and manager_id == user_id

    async def list_managed_team_ids(self, manager_id: int) -> set[int]:
        """Every team_id this user manages in this organization — a
        single, lightweight columns-only query (no eager-loading), used to
        scope "All Tasks" (and any other org-wide listing) down to a plain
        Team Manager's own teams instead of returning every task in the
        organization. Returns an empty set (never None) for a manager of
        zero teams, so callers can tell "restricted, but to nothing" apart
        from "not restricted at all" (see app.api.routes.tasks.list_tasks)."""
        stmt = select(Team.id).where(Team.team_manager_id == manager_id, Team.organization_id == self.org_id)
        result = await self.db.execute(stmt)
        return set(result.scalars().all())

    async def has_access(self, team_id: int, user_id: int) -> bool:
        """True if `user_id` is this team's manager OR a TeamMembership
        member of it — the single access rule app.core.team_access enforces
        for every team-scoped domain (Rocks, Issues, KPIs, Team News, ...),
        not just the team itself. A cheap two-column-select check, no
        relationship eager-loading required."""
        stmt = select(Team.team_manager_id).where(Team.id == team_id, Team.organization_id == self.org_id)
        manager_id = (await self.db.execute(stmt)).scalar_one_or_none()
        if manager_id is None:
            return False  # team doesn't exist (or isn't in this org) — let the caller's own 404 check handle that
        if manager_id == user_id:
            return True
        member_stmt = select(TeamMembership.id).where(TeamMembership.team_id == team_id, TeamMembership.user_id == user_id)
        return (await self.db.execute(member_stmt)).scalar_one_or_none() is not None

    async def filter_accessible_team_ids(self, team_ids: list[int], user_id: int) -> set[int]:
        """Inline-assignee-dropdown bug-fix follow-up: of `team_ids`, which
        ones `user_id` has team access to (manager OR TeamMembership member
        — the exact same rule `has_access()`/`require_team_access()` use
        for a single team, applied in bulk instead of once per id). Two
        queries at most, regardless of how many team_ids are passed in —
        never one `has_access()` call per team."""
        if not team_ids:
            return set()
        managed_stmt = select(Team.id).where(
            Team.id.in_(team_ids), Team.organization_id == self.org_id, Team.team_manager_id == user_id,
        )
        managed_ids = set((await self.db.execute(managed_stmt)).scalars().all())
        remaining_ids = [tid for tid in team_ids if tid not in managed_ids]
        member_ids: set[int] = set()
        if remaining_ids:
            member_stmt = select(TeamMembership.team_id).where(
                TeamMembership.team_id.in_(remaining_ids), TeamMembership.user_id == user_id,
            )
            member_ids = set((await self.db.execute(member_stmt)).scalars().all())
        return managed_ids | member_ids

    async def filter_existing_team_ids(self, team_ids: list[int]) -> set[int]:
        """Org-scoped existence check only — no per-user access
        restriction — used for the Owner/Admin path of the bulk
        assignable-users lookup, where every team in the org is already
        authorized (mirrors Admin/Owner being unrestricted everywhere else
        team access is checked)."""
        if not team_ids:
            return set()
        stmt = select(Team.id).where(Team.id.in_(team_ids), Team.organization_id == self.org_id)
        return set((await self.db.execute(stmt)).scalars().all())

    async def list_assignable_members_bulk(self, team_ids: list[int]) -> dict[int, list[User]]:
        """Bulk equivalent of list_assignable_members() below, for however
        many distinct teams a Task list's currently-visible rows belong to
        — ONE query, regardless of how many team_ids are passed in (never
        one `list_assignable_members()` call per team, and never one per
        Task row — see the inline-assignee-dropdown bug-fix follow-up).
        Same eligibility rule: ACTIVE members of each team, Client always
        excluded via the authoritative OrganizationMembership.role (never
        the legacy User.role column)."""
        if not team_ids:
            return {}
        stmt = (
            select(TeamMembership.team_id, User)
            .join(User, User.id == TeamMembership.user_id)
            .join(OrganizationMembership, OrganizationMembership.user_id == User.id)
            .where(
                TeamMembership.team_id.in_(team_ids),
                OrganizationMembership.organization_id == self.org_id,
                OrganizationMembership.role != CLIENT,
                OrganizationMembership.is_active.is_(True),
                User.is_active.is_(True),
            )
            .order_by(User.full_name.asc())
        )
        result = await self.db.execute(stmt)
        members_by_team: dict[int, list[User]] = {}
        for team_id, user in result.all():
            members_by_team.setdefault(team_id, []).append(user)
        return members_by_team

    async def list_assignable_members(self, team_id: int) -> list[User]:
        """Task Assignee bug-fix follow-up: the eligible-assignee set for a
        Team Task — ACTIVE members of THIS exact team, excluding Client
        (Rule A/B). Deliberately joins OrganizationMembership (the
        authoritative, organization-scoped role — see app.core.org_roles's
        module docstring) rather than filtering on the legacy, possibly
        stale `User.role` column that `TeamDetailRead.members` currently
        exposes; that column is a display default only and cannot be
        trusted to reflect a member's real per-org role (e.g. a Client
        invited into this organization may still carry a leftover
        `User.role` of "team_member" from account creation).

        The team's manager is already guaranteed to also hold a
        TeamMembership row (see create()/update() below, which always add
        team_manager_id to the membership set), so this single join
        covers the manager too — no separate UNION needed."""
        stmt = (
            select(User)
            .join(TeamMembership, TeamMembership.user_id == User.id)
            .join(OrganizationMembership, OrganizationMembership.user_id == User.id)
            .where(
                TeamMembership.team_id == team_id,
                OrganizationMembership.organization_id == self.org_id,
                OrganizationMembership.role != CLIENT,
                OrganizationMembership.is_active.is_(True),
                User.is_active.is_(True),
            )
            .order_by(User.full_name.asc())
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().unique().all())

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
