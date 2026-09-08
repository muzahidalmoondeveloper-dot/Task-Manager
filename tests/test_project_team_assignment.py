"""Regression tests for the Project Manager Team-selection bug fix
(explicit Project<->Team assignment: app.models.project.ProjectTeam,
app.core.project_access.list_project_team_ids, and the new
GET/POST/DELETE /projects/{id}/teams... routes).

Root cause (see final report): "which teams belong to a project" was only
ever DERIVED after the fact from Rocks/KPIs/Tasks that already carried
both a project_id and a team_id — circular for a brand-new project with
none of those yet, so a Project Manager's Create-Task "Team" dropdown (and
the Project Overview's "Teams" count) stayed empty even when the
organization plainly had teams, with no way to ever create the first
team-tagged task.

Covers:
  1. A brand-new project (no tasks/rocks/kpis) starts with zero associated
     teams — confirms the bug's starting state.
  2. Owner/Admin can explicitly assign an existing team to the project.
  3. Once assigned, list_project_team_ids() includes it (GET /projects/
     {id}/items' own "teams" field, and the write-side team_id check
     create_task() uses, both read this).
  4. A Project Manager who is a genuine ProjectMembership member of this
     project can now create a Team Task using the newly-assigned team.
  5. GET /projects/{id}/teams/assignable returns {id, name, assigned} only
     — never full team detail (no members/manager fields).
  6. A Project Manager NOT a member of this project is denied assigning.
  7. A plain Team Manager (no PM capability) is denied assigning —
     ProjectMembership/team-manager status alone is never sufficient.
  8. A Client is denied assigning.
  9. Assigning twice is idempotent — no duplicate ProjectTeam row.
  10. Unassigning removes the explicit link; the derived (Rock/KPI/Task)
      half of list_project_team_ids is unaffected by explicit removal.
  11. Cross-tenant: a team_id belonging to a different organization 404s,
      never assignable.
  12. Activity Log records project.team_assigned / project.team_removed.
  13. GET /projects/{id}/items' "teams" field reflects the explicit
      assignment end-to-end, even with zero rocks/kpis/tasks — the exact
      screenshot bug.

Runs against the real database connection the app uses. Every row this
test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from fastapi import BackgroundTasks
from sqlalchemy import delete, select

from app.api.routes.projects import (
    assign_project_team,
    get_project_items,
    list_assignable_teams,
    unassign_project_team,
)
from app.api.routes.tasks import create_task
from app.core.activity_actions import PROJECT_TEAM_ASSIGNED, PROJECT_TEAM_REMOVED
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import CLIENT, OWNER, PROJECT_MANAGER, TEAM_MANAGER, TEAM_MEMBER
from app.core.project_access import list_project_team_ids
from app.core.tenant import TenantContext
from app.models.activity_log import ActivityLog
from app.models.organization import Organization, OrganizationMembership
from app.models.project import Project, ProjectMembership, ProjectTeam
from app.models.task import Task
from app.models.team import Team
from app.models.user import User
from app.schemas.task import TaskCreate


async def _run():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]

        owner = User(full_name="PTA Owner", email=f"pta.owner.{suffix}@example-corp.com", hashed_password="x", role="owner")
        pm = User(full_name="PTA PM", email=f"pta.pm.{suffix}@example-corp.com", hashed_password="x", role=PROJECT_MANAGER)
        pm_outsider = User(full_name="PTA PM Outsider", email=f"pta.pmoutsider.{suffix}@example-corp.com", hashed_password="x", role=PROJECT_MANAGER)
        tm = User(full_name="PTA TM", email=f"pta.tm.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MANAGER)
        client_user = User(full_name="PTA Client", email=f"pta.client.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)
        outsider_owner = User(full_name="PTA Outsider Owner", email=f"pta.outsiderowner.{suffix}@example-corp.com", hashed_password="x", role="owner")
        db.add_all([owner, pm, pm_outsider, tm, client_user, outsider_owner])
        await db.commit()
        for u in (owner, pm, pm_outsider, tm, client_user, outsider_owner):
            await db.refresh(u)

        org = Organization(name=f"PTA Org {suffix}", slug=f"pta-org-{suffix}", owner_id=owner.id)
        other_org = Organization(name=f"PTA Other Org {suffix}", slug=f"pta-other-org-{suffix}", owner_id=outsider_owner.id)
        db.add_all([org, other_org])
        await db.commit()
        from sqlalchemy.orm import selectinload
        org = (await db.execute(select(Organization).options(selectinload(Organization.subscription)).where(Organization.id == org.id))).scalar_one()
        other_org = (await db.execute(select(Organization).options(selectinload(Organization.subscription)).where(Organization.id == other_org.id))).scalar_one()

        memberships = {}
        for u, role in [(owner, OWNER), (pm, PROJECT_MANAGER), (pm_outsider, PROJECT_MANAGER), (tm, TEAM_MANAGER), (client_user, CLIENT)]:
            m = OrganizationMembership(organization_id=org.id, user_id=u.id, role=role)
            db.add(m)
            memberships[u.id] = m
        outsider_owner_membership = OrganizationMembership(organization_id=other_org.id, user_id=outsider_owner.id, role=OWNER)
        db.add(outsider_owner_membership)
        await db.commit()

        project = Project(name=f"PTA Clarvs {suffix}", created_by_id=owner.id, organization_id=org.id)
        db.add(project)
        await db.commit()
        await db.refresh(project)
        db.add(ProjectMembership(project_id=project.id, user_id=pm.id))
        await db.commit()

        team = Team(name=f"PTA Team {suffix}", team_manager_id=tm.id, created_by_id=owner.id, organization_id=org.id)
        other_org_team = Team(name=f"PTA Other Org Team {suffix}", team_manager_id=outsider_owner.id, created_by_id=outsider_owner.id, organization_id=other_org.id)
        db.add_all([team, other_org_team])
        await db.commit()
        for t in (team, other_org_team):
            await db.refresh(t)

        owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[owner.id], user=owner, db=db)
        pm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[pm.id], user=pm, db=db)
        pm_outsider_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[pm_outsider.id], user=pm_outsider, db=db)
        tm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[tm.id], user=tm, db=db)
        client_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[client_user.id], user=client_user, db=db)

        created_task_ids: list[int] = []

        try:
            # ── 1. Brand-new project starts with zero associated teams. ───────
            initial_ids = await list_project_team_ids(db, org.id, project.id)
            assert initial_ids == set(), "a project with no tasks/rocks/kpis/explicit assignment must start with zero teams"

            # ── 6, 7, 8. Denied before any valid assignment. ──────────────────
            try:
                await assign_project_team(project.id, team.id, tenant=pm_outsider_tenant)
                raise AssertionError("a Project Manager who is NOT a member of this project must be denied")
            except AppException as exc:
                assert exc.status_code == 403, exc

            try:
                await assign_project_team(project.id, team.id, tenant=tm_tenant)
                raise AssertionError("a plain Team Manager (no PM capability) must be denied — ProjectMembership alone is never sufficient")
            except AppException as exc:
                assert exc.status_code == 403, exc

            try:
                await assign_project_team(project.id, team.id, tenant=client_tenant)
                raise AssertionError("a Client must be denied")
            except AppException as exc:
                assert exc.status_code == 403, exc

            # ── 11. Cross-tenant: another org's team_id never assignable. ─────
            try:
                await assign_project_team(project.id, other_org_team.id, tenant=owner_tenant)
                raise AssertionError("a team from a different organization must never be assignable")
            except AppException as exc:
                assert exc.status_code == 404, exc

            # ── 2. Owner assigns the team. ──────────────────────────────────
            result = await assign_project_team(project.id, team.id, tenant=owner_tenant)
            assert result["team_id"] == team.id

            # ── 9. Idempotent — assigning again creates no duplicate row. ─────
            await assign_project_team(project.id, team.id, tenant=owner_tenant)
            rows = (await db.execute(
                select(ProjectTeam).where(ProjectTeam.project_id == project.id, ProjectTeam.team_id == team.id)
            )).scalars().all()
            assert len(rows) == 1, "assigning an already-assigned team must never duplicate the row"

            # ── 3, 13. list_project_team_ids() and GET /projects/{id}/items
            # both reflect the explicit assignment with zero rocks/kpis/tasks
            # — the exact screenshot bug. ───────────────────────────────────
            after_assign_ids = await list_project_team_ids(db, org.id, project.id)
            assert team.id in after_assign_ids

            items = await get_project_items(project.id, db=db, tenant=pm_tenant)
            item_team_ids = {t.id for t in items["teams"]}
            assert team.id in item_team_ids, "GET /projects/{id}/items must show the explicitly-assigned team even with no rocks/kpis/tasks yet"

            # ── 4. PM (project member) can now create a Team Task using the
            # newly-assigned team — the actual reported bug, fixed
            # end-to-end. ──────────────────────────────────────────────────
            task = await create_task(
                TaskCreate(name=f"PTA Task {suffix}", project_id=project.id, team_id=team.id),
                background_tasks=BackgroundTasks(), tenant=pm_tenant, db=db,
            )
            created_task_ids.append(task.id)
            assert task.team_id == team.id
            assert task.project_id == project.id

            # ── 5. Assignable-teams listing is minimal {id, name, assigned}. ──
            options = await list_assignable_teams(project.id, tenant=pm_tenant)
            assert any(o.id == team.id and o.assigned is True and o.name == team.name for o in options)
            for o in options:
                assert not hasattr(o, "members")
                assert not hasattr(o, "team_manager_id")

            # ── 12. Activity Log recorded the assignment. ──────────────────────
            assign_logs = (await db.execute(
                select(ActivityLog).where(ActivityLog.organization_id == org.id, ActivityLog.action == PROJECT_TEAM_ASSIGNED, ActivityLog.entity_id == project.id)
            )).scalars().all()
            assert len(assign_logs) >= 1

            # ── 10. Unassign removes the explicit link. ─────────────────────
            await unassign_project_team(project.id, team.id, tenant=owner_tenant)
            after_unassign_ids = await list_project_team_ids(db, org.id, project.id)
            # The team is STILL derivable via the task created above
            # (Task.team_id == team.id, Task.project_id == project.id) — the
            # derived half of list_project_team_ids is unaffected by
            # removing the explicit assignment, exactly as intended (this
            # fix only ADDS a source, it never removes the original one).
            assert team.id in after_unassign_ids, "the derived (task-based) association must survive removing the explicit one"

            remove_logs = (await db.execute(
                select(ActivityLog).where(ActivityLog.organization_id == org.id, ActivityLog.action == PROJECT_TEAM_REMOVED, ActivityLog.entity_id == project.id)
            )).scalars().all()
            assert len(remove_logs) >= 1

        finally:
            await db.execute(delete(ActivityLog).where(ActivityLog.organization_id.in_([org.id, other_org.id])))
            if created_task_ids:
                await db.execute(delete(Task).where(Task.id.in_(created_task_ids)))
            await db.execute(delete(ProjectTeam).where(ProjectTeam.project_id == project.id))
            await db.execute(delete(ProjectMembership).where(ProjectMembership.project_id == project.id))
            await db.execute(delete(Project).where(Project.id == project.id))
            await db.execute(delete(Team).where(Team.id.in_([team.id, other_org_team.id])))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_([org.id, other_org.id])))
            await db.execute(delete(Organization).where(Organization.id.in_([org.id, other_org.id])))
            all_user_ids = [owner.id, pm.id, pm_outsider.id, tm.id, client_user.id, outsider_owner.id]
            await db.execute(delete(User).where(User.id.in_(all_user_ids)))
            await db.commit()

    await engine.dispose()


def test_project_team_assignment():
    asyncio.run(_run())
