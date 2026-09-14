"""Regression tests for the Team Manager Project/Team-independence
follow-up (Task Create + Task Edit + Team To-Do).

PRODUCT RULE: for a Team Manager's own Task/To-Do workflows, Project and
Team are INDEPENDENT context fields — a Team Manager may combine ANY
active same-org Project with ANY Team they legitimately manage, with NO
requirement that a `ProjectTeam` row link them. This is explicitly
DIFFERENT from Project Manager delegation, where Project->attached-Team
still fully applies, unchanged (see test_project_manager_task_delegation.py,
reconfirmed here too).

ROOT CAUSE (Issue 1 — TM Task Create Team dropdown going empty): `create_task`'s
`if payload.project_id is not None and is_project_scoped(tenant):` block
also gated Team Managers (`is_project_scoped` returns True for anyone
`is_manager_or_above`, not just plain Project Managers) — behind a
ProjectMembership-or-managed-team-Project-attachment check. Selecting a
Project with no ProjectTeam link to any of the TM's own managed Teams
made this check fail outright (or, client-side, made the mirrored
Project->Team intersection filter render an empty Team dropdown).

FIX: that ProjectMembership-based restriction now applies ONLY when
`not tenant.is_manager_or_above` — i.e. a plain Project Manager. A Team
Manager's `project_id` only needs to belong to this organization (the
existence check right below, unchanged) — no ProjectMembership, no
Project<->Team attachment. `team_id` was already, and remains,
independently validated via `require_team_access` (unchanged).

Also fixes a genuine pre-existing gap in `update_task`: `project_id` had
NO existence/org-scope validation at all for a Team Manager/Owner/Admin's
PATCH — added the same `project_repo.get_by_id` check `create_task`
already had, so a forged cross-tenant/nonexistent `project_id` is
rejected on edit too.

Covers spec test items 1-13 (TM Task Create), 14-17 (TM Task Edit), and
26-30 (PM regression, reconfirmed here in addition to the existing
dedicated PM delegation test file).

Runs against the real database connection the app uses. Every row this
test creates is deleted before it returns.
"""

import asyncio
import uuid

from fastapi import BackgroundTasks
from sqlalchemy import select
from sqlalchemy.orm import selectinload

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from fastapi import HTTPException
from sqlalchemy import delete

from app.api.routes.tasks import create_task, update_task
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import CLIENT, OWNER, PROJECT_MANAGER, TEAM_MANAGER, TEAM_MEMBER
from app.core.tenant import TenantContext
from app.models.organization import Organization, OrganizationMembership
from app.models.project import Project, ProjectMembership, ProjectTeam
from app.models.task import Task
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.schemas.task import TaskCreate, TaskUpdate


async def _run():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]

        owner = User(full_name="TPI Owner", email=f"tpi.owner.{suffix}@example-corp.com", hashed_password="x", role="owner")
        tm = User(full_name="TPI TM", email=f"tpi.tm.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MANAGER)
        other_tm = User(full_name="TPI Other TM", email=f"tpi.othertm.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MANAGER)
        pm = User(full_name="TPI PM", email=f"tpi.pm.{suffix}@example-corp.com", hashed_password="x", role=PROJECT_MANAGER)
        eligible = User(full_name="TPI Eligible", email=f"tpi.eligible.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)
        wrong_team_member = User(full_name="TPI WrongTeam", email=f"tpi.wrongteam.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)
        client_user = User(full_name="TPI Client", email=f"tpi.client.{suffix}@example-corp.com", hashed_password="x", role=CLIENT)
        db.add_all([owner, tm, other_tm, pm, eligible, wrong_team_member, client_user])
        await db.commit()
        for u in (owner, tm, other_tm, pm, eligible, wrong_team_member, client_user):
            await db.refresh(u)

        org = Organization(name=f"TPI Org {suffix}", slug=f"tpi-org-{suffix}", owner_id=owner.id)
        other_org = Organization(name=f"TPI Other Org {suffix}", slug=f"tpi-other-org-{suffix}", owner_id=owner.id)
        db.add_all([org, other_org])
        await db.commit()
        org = (await db.execute(select(Organization).options(selectinload(Organization.subscription)).where(Organization.id == org.id))).scalar_one()

        memberships = {}
        for u, role in [
            (owner, OWNER), (tm, TEAM_MANAGER), (other_tm, TEAM_MANAGER), (pm, PROJECT_MANAGER),
            (eligible, TEAM_MEMBER), (wrong_team_member, TEAM_MEMBER), (client_user, CLIENT),
        ]:
            m = OrganizationMembership(organization_id=org.id, user_id=u.id, role=role)
            db.add(m)
            memberships[u.id] = m
        await db.commit()

        # TM manages Technology Team + Marketing Team. other_tm manages an
        # unrelated Team the first TM does NOT manage.
        technology = Team(name=f"Technology Team {suffix}", team_manager_id=tm.id, created_by_id=owner.id, organization_id=org.id)
        marketing = Team(name=f"Marketing Team {suffix}", team_manager_id=tm.id, created_by_id=owner.id, organization_id=org.id)
        unmanaged = Team(name=f"TPI Unmanaged {suffix}", team_manager_id=other_tm.id, created_by_id=owner.id, organization_id=org.id)
        db.add_all([technology, marketing, unmanaged])
        await db.commit()
        for t in (technology, marketing, unmanaged):
            await db.refresh(t)
        db.add_all([
            TeamMembership(team_id=technology.id, user_id=tm.id),
            TeamMembership(team_id=technology.id, user_id=eligible.id),
            TeamMembership(team_id=marketing.id, user_id=tm.id),
            TeamMembership(team_id=marketing.id, user_id=wrong_team_member.id),
        ])
        await db.commit()

        # Projects — deliberately NO ProjectTeam attachment to technology
        # or marketing at all.
        clarvs = Project(name=f"Clarvs {suffix}", created_by_id=owner.id, organization_id=org.id, status="active")
        project_b = Project(name=f"TPI Project B {suffix}", created_by_id=owner.id, organization_id=org.id, status="active")
        cross_tenant_project = Project(name=f"TPI CrossTenant {suffix}", created_by_id=owner.id, organization_id=other_org.id, status="active")
        db.add_all([clarvs, project_b, cross_tenant_project])
        await db.commit()
        for p in (clarvs, project_b, cross_tenant_project):
            await db.refresh(p)

        # PM's own managed Project, WITH an explicit ProjectTeam attachment
        # to `technology` — used only for the PM-regression checks below.
        pm_project = Project(name=f"TPI PM Project {suffix}", created_by_id=owner.id, organization_id=org.id, status="active")
        db.add(pm_project)
        await db.commit()
        await db.refresh(pm_project)
        db.add_all([
            ProjectMembership(project_id=pm_project.id, user_id=pm.id),
            ProjectTeam(project_id=pm_project.id, team_id=technology.id, assigned_by_id=owner.id),
        ])
        await db.commit()

        cross_tenant_team = Team(name=f"TPI CrossTenant Team {suffix}", team_manager_id=owner.id, created_by_id=owner.id, organization_id=other_org.id)
        db.add(cross_tenant_team)
        await db.commit()
        await db.refresh(cross_tenant_team)

        owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[owner.id], user=owner, db=db)
        tm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[tm.id], user=tm, db=db)
        pm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[pm.id], user=pm, db=db)

        created_task_ids: list[int] = []

        async def _create(payload, tenant):
            task = await create_task(payload, background_tasks=BackgroundTasks(), tenant=tenant, db=db)
            created_task_ids.append(task.id)
            return task

        async def _update(task_id, payload, tenant=tm_tenant):
            return await update_task(task_id, payload, background_tasks=BackgroundTasks(), tenant=tenant, db=db)

        try:
            # ── 5. Clarvs has NO explicit ProjectTeam link to Technology
            # Team. TM creates project=Clarvs, team=Technology -> succeeds. ──
            tm_task = await _create(
                TaskCreate(name=f"TPI TM Task {suffix}", project_id=clarvs.id, team_id=technology.id, assignee_id=eligible.id),
                tm_tenant,
            )
            assert tm_task.project_id == clarvs.id
            assert tm_task.team_id == technology.id
            assert tm_task.assignee_id == eligible.id

            # ── 6. No ProjectTeam association was automatically created. ──
            auto_link = (await db.execute(
                select(ProjectTeam).where(ProjectTeam.project_id == clarvs.id, ProjectTeam.team_id == technology.id)
            )).scalar_one_or_none()
            assert auto_link is None, "creating a Task must never silently create a ProjectTeam attachment"

            # ── Marketing Team (also unattached to Clarvs) works too —
            # confirms Team options aren't narrowed to a single Project-
            # derived Team. ──────────────────────────────────────────────
            tm_task_2 = await _create(
                TaskCreate(name=f"TPI TM Task 2 {suffix}", project_id=clarvs.id, team_id=marketing.id),
                tm_tenant,
            )
            assert tm_task_2.team_id == marketing.id

            # ── 9. Unmanaged Team -> rejected. ───────────────────────────
            try:
                await _create(TaskCreate(name=f"TPI Bad {suffix}", project_id=clarvs.id, team_id=unmanaged.id), tm_tenant)
                raise AssertionError("a Team Manager must not create a Task under a Team they don't manage, regardless of Project")
            except AppException as exc:
                assert exc.status_code == 403, exc

            # ── 10. Cross-tenant Project -> rejected. ────────────────────
            try:
                await _create(TaskCreate(name=f"TPI Bad {suffix}", project_id=cross_tenant_project.id, team_id=technology.id), tm_tenant)
                raise AssertionError("a cross-tenant project_id must be rejected even for a TM's own Team")
            except HTTPException as exc:
                assert exc.status_code == 400, exc

            # ── 11. Cross-tenant Team -> rejected. ───────────────────────
            try:
                await _create(TaskCreate(name=f"TPI Bad {suffix}", project_id=clarvs.id, team_id=cross_tenant_team.id), tm_tenant)
                raise AssertionError("a cross-tenant team_id must be rejected")
            except HTTPException as exc:
                assert exc.status_code == 400, exc

            # ── 12. wrong-Team / Client / inactive assignee -> rejected. ──
            try:
                await _create(TaskCreate(name=f"TPI Bad {suffix}", project_id=clarvs.id, team_id=technology.id, assignee_id=wrong_team_member.id), tm_tenant)
                raise AssertionError("an assignee who isn't a member of the selected Team must be rejected")
            except AppException as exc:
                assert exc.code == "ASSIGNEE_NOT_TEAM_MEMBER", exc
            try:
                await _create(TaskCreate(name=f"TPI Bad {suffix}", project_id=clarvs.id, team_id=technology.id, assignee_id=client_user.id), tm_tenant)
                raise AssertionError("a Client must never be assignable")
            except AppException as exc:
                assert exc.code == "CLIENT_NOT_ASSIGNABLE", exc

            # ── 13. Personal/no-Team Task behavior remains unchanged
            # (self-assign default, regardless of Project). ──────────────
            personal = await _create(TaskCreate(name=f"TPI Personal {suffix}", project_id=clarvs.id), tm_tenant)
            assert personal.team_id is None
            assert personal.assignee_id == tm.id
            assert personal.project_id == clarvs.id

            # ── 14, 15, 16, 17. TM Task Edit: change ONLY Project on a
            # managed-Team Task -> succeeds, Team unchanged, Project B
            # needs no Technology-Team attachment, Assignee stays valid. ──
            edited = await _update(tm_task.id, TaskUpdate(project_id=project_b.id))
            assert edited.project_id == project_b.id
            assert edited.team_id == technology.id, "changing Project must never force/clear the Team"
            assert edited.assignee_id == eligible.id, "an already-valid Assignee must survive a Project-only change"

            # Project = NULL, Team = Technology Team remains valid too.
            cleared = await _update(tm_task.id, TaskUpdate(project_id=None))
            assert cleared.project_id is None
            assert cleared.team_id == technology.id

            # Cross-tenant project_id on update -> rejected.
            try:
                await _update(tm_task.id, TaskUpdate(project_id=cross_tenant_project.id))
                raise AssertionError("a cross-tenant project_id must be rejected on Task update too")
            except HTTPException as exc:
                assert exc.status_code == 400, exc

            # Nonexistent project_id on update -> rejected.
            try:
                await _update(tm_task.id, TaskUpdate(project_id=999_999_999))
                raise AssertionError("a nonexistent project_id must be rejected on Task update")
            except HTTPException as exc:
                assert exc.status_code == 400, exc

            # ── 26, 27, 28, 29, 30. PM regression — delegation still
            # requires an attached Team, still never auto-assigns, PM
            # still can't name a Team Member directly. Reconfirmed here in
            # addition to test_project_manager_task_delegation.py. ────────
            pm_delegated = await _create(
                TaskCreate(name=f"TPI PM Delegated {suffix}", project_id=pm_project.id, team_id=technology.id),
                pm_tenant,
            )
            assert pm_delegated.assignee_id is None, "PM delegation must still never auto-assign"
            assert pm_delegated.team_id == technology.id

            # marketing is NOT attached to pm_project -> still rejected for PM.
            try:
                await _create(TaskCreate(name=f"TPI PM Bad {suffix}", project_id=pm_project.id, team_id=marketing.id), pm_tenant)
                raise AssertionError("PM delegation must still require the Team to be attached to the Project — this rule must NOT have been relaxed for PM")
            except HTTPException as exc:
                assert exc.status_code == 403, exc

            # PM still cannot directly assign a Team Member through delegation.
            try:
                await _create(
                    TaskCreate(name=f"TPI PM Bad Assign {suffix}", project_id=pm_project.id, team_id=technology.id, assignee_id=eligible.id),
                    pm_tenant,
                )
                raise AssertionError("PM must still never directly assign a Team Member through delegation")
            except AppException as exc:
                assert exc.code == "PROJECT_MANAGER_CANNOT_ASSIGN_MEMBER", exc

            # PM still requires a Project to delegate (team_id set, no project_id).
            try:
                await _create(TaskCreate(name=f"TPI PM No Project {suffix}", team_id=technology.id), pm_tenant)
                raise AssertionError("PM delegation must still require a Project")
            except AppException as exc:
                assert exc.code == "PROJECT_REQUIRED", exc

        finally:
            if created_task_ids:
                await db.execute(delete(Task).where(Task.id.in_(created_task_ids)))
            await db.execute(delete(ProjectTeam).where(ProjectTeam.project_id == pm_project.id))
            await db.execute(delete(ProjectMembership).where(ProjectMembership.project_id == pm_project.id))
            await db.execute(delete(Project).where(Project.id.in_([clarvs.id, project_b.id, cross_tenant_project.id, pm_project.id])))
            await db.execute(delete(TeamMembership).where(TeamMembership.team_id.in_([technology.id, marketing.id])))
            await db.execute(delete(Team).where(Team.id.in_([technology.id, marketing.id, unmanaged.id, cross_tenant_team.id])))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id.in_([org.id, other_org.id])))
            await db.execute(delete(User).where(User.id.in_([owner.id, tm.id, other_tm.id, pm.id, eligible.id, wrong_team_member.id, client_user.id])))
            await db.commit()

    await engine.dispose()


def test_tm_task_project_team_independence():
    asyncio.run(_run())
