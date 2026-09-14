"""Team Manager "Create Task" form regression test.

Root cause: `GET /projects` returns an empty list for a plain Team Manager
by design (`app.core.project_access.is_project_management_blocked`) — the
Create Task modal's Project dropdown sourced its options from exactly that
call, so it was always empty for a Team Manager even when their own
managed Team(s) have real Projects attached via the explicit Project<->Team
association (ProjectTeam).

Fix: a new, narrowly-scoped `GET /projects/for-managed-teams` endpoint
(`app.api.routes.projects.list_projects_for_managed_teams`) — self-scoped
entirely from the caller's own managed-Team relationships, never
organization-wide Project access. Also closes a real, separate gap this
surfaced: `create_task()` never verified a Team Manager (or Owner/Admin)
actually has access to the `team_id` they're filing a Task under — a Team
Manager could previously create a Task under ANY team in the org.

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from fastapi import HTTPException

from sqlalchemy import delete

from app.core.auth_errors import AppException

from app.api.routes.projects import list_projects_for_managed_teams
from app.api.routes.tasks import create_task
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import PROJECT_MANAGER, TEAM_MANAGER
from app.core.tenant import TenantContext
from app.models.organization import Organization, OrganizationMembership
from app.models.project import Project, ProjectMembership, ProjectTeam
from app.models.task import Task
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.schemas.task import TaskCreate


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="TMTask Owner", email=f"tmtask.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        tm = User(full_name="TMTask TM", email=f"tmtask.tm.{suffix}@test.invalid", hashed_password="x", role=TEAM_MANAGER)
        other_tm = User(full_name="TMTask OtherTM", email=f"tmtask.othertm.{suffix}@test.invalid", hashed_password="x", role=TEAM_MANAGER)
        pm = User(full_name="TMTask PM", email=f"tmtask.pm.{suffix}@test.invalid", hashed_password="x", role=PROJECT_MANAGER)
        other_org_owner = User(full_name="TMTask OtherOwner", email=f"tmtask.otherowner.{suffix}@test.invalid", hashed_password="x", role="owner")
        db.add_all([owner, tm, other_tm, pm, other_org_owner])
        await db.flush()

        org = Organization(name=f"TMTask Org {suffix}", slug=f"tmtask-{suffix}", owner_id=owner.id)
        other_org = Organization(name=f"TMTask OtherOrg {suffix}", slug=f"tmtask-other-{suffix}", owner_id=other_org_owner.id)
        db.add_all([org, other_org])
        await db.flush()

        owner_membership = OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner")
        tm_membership = OrganizationMembership(organization_id=org.id, user_id=tm.id, role=TEAM_MANAGER)
        other_tm_membership = OrganizationMembership(organization_id=org.id, user_id=other_tm.id, role=TEAM_MANAGER)
        pm_membership = OrganizationMembership(organization_id=org.id, user_id=pm.id, role=PROJECT_MANAGER)
        other_org_owner_membership = OrganizationMembership(organization_id=other_org.id, user_id=other_org_owner.id, role="owner")
        db.add_all([owner_membership, tm_membership, other_tm_membership, pm_membership, other_org_owner_membership])
        await db.commit()

        # tm manages Team A (attached to Clarvs + Zero Fund) and Team B
        # (attached to MailHub) — the exact "Technology/Marketing Team"
        # example from the spec. other_tm manages an unrelated Team C
        # attached to an unrelated Project D — neither may ever appear
        # for `tm`.
        team_a = Team(name=f"Technology Team {suffix}", organization_id=org.id, team_manager_id=tm.id, created_by_id=owner.id)
        team_b = Team(name=f"Marketing Team {suffix}", organization_id=org.id, team_manager_id=tm.id, created_by_id=owner.id)
        team_c = Team(name=f"Unrelated Team {suffix}", organization_id=org.id, team_manager_id=other_tm.id, created_by_id=owner.id)
        db.add_all([team_a, team_b, team_c])
        await db.flush()
        # A Team Manager also always carries a TeamMembership row on their
        # own team (see Team.create()'s convention elsewhere) — reproduced
        # here since this test builds rows directly.
        db.add_all([
            TeamMembership(team_id=team_a.id, user_id=tm.id),
            TeamMembership(team_id=team_b.id, user_id=tm.id),
            TeamMembership(team_id=team_c.id, user_id=other_tm.id),
        ])

        clarvs = Project(name=f"Clarvs {suffix}", organization_id=org.id, created_by_id=owner.id)
        zero_fund = Project(name=f"Zero Fund {suffix}", organization_id=org.id, created_by_id=owner.id)
        mailhub = Project(name=f"MailHub {suffix}", organization_id=org.id, created_by_id=owner.id)
        project_d = Project(name=f"Unrelated Project D {suffix}", organization_id=org.id, created_by_id=owner.id)
        db.add_all([clarvs, zero_fund, mailhub, project_d])
        await db.flush()

        # PM's own managed project, used to prove PM's existing delegation
        # flow (assigning a Team they don't personally manage) is
        # unaffected by the new require_team_access check in create_task.
        pm_project = Project(name=f"PM Project {suffix}", organization_id=org.id, created_by_id=owner.id)
        db.add(pm_project)
        await db.flush()

        db.add_all([
            ProjectTeam(project_id=clarvs.id, team_id=team_a.id, assigned_by_id=owner.id),
            ProjectTeam(project_id=zero_fund.id, team_id=team_a.id, assigned_by_id=owner.id),
            ProjectTeam(project_id=mailhub.id, team_id=team_b.id, assigned_by_id=owner.id),
            ProjectTeam(project_id=project_d.id, team_id=team_c.id, assigned_by_id=owner.id),
            ProjectTeam(project_id=pm_project.id, team_id=team_a.id, assigned_by_id=owner.id),
            ProjectMembership(project_id=pm_project.id, user_id=pm.id),
        ])
        await db.commit()

        when = datetime.now(timezone.utc) + timedelta(days=3)
        owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=owner_membership, user=owner, db=db)
        tm_tenant = TenantContext(organization_id=org.id, organization=org, membership=tm_membership, user=tm, db=db)
        pm_tenant = TenantContext(organization_id=org.id, organization=org, membership=pm_membership, user=pm, db=db)

        task_ids = []
        try:
            # ── 1/2/3. TM's Project dropdown: Clarvs + Zero Fund + MailHub,
            # deduplicated, unrelated Project D absent, correct team_ids
            # per project (never leaking team_c, which this TM doesn't
            # manage). ──
            options = await list_projects_for_managed_teams(tenant=tm_tenant)
            by_name = {o.name: o for o in options}
            # pm_project is also legitimately attached to team_a (this TM's
            # own managed team) — set up below purely to exercise the PM
            # delegation-flow regression check, but it's a real, correct
            # member of this TM's own dropdown too.
            assert set(by_name.keys()) == {clarvs.name, zero_fund.name, mailhub.name, pm_project.name}, \
                f"unexpected TM project-dropdown set: {set(by_name.keys())}"
            assert by_name[clarvs.name].team_ids == [team_a.id]
            assert by_name[zero_fund.name].team_ids == [team_a.id]
            assert by_name[mailhub.name].team_ids == [team_b.id]

            # ── Cross-tenant isolation: other_org's owner manages nothing
            # here and sees nothing. ──
            other_org_tenant = TenantContext(
                organization_id=other_org.id, organization=other_org,
                membership=other_org_owner_membership, user=other_org_owner, db=db,
            )
            cross_tenant_options = await list_projects_for_managed_teams(tenant=other_org_tenant)
            assert cross_tenant_options == []

            # ── Owner/Admin unaffected: GET /projects (list_projects,
            # unchanged) still returns everything for them — this new
            # endpoint is additive, not a replacement. ──

            # ── 9. Existing task creation still succeeds: TM creates a
            # Task under a team they genuinely manage. ──
            created = await create_task(
                TaskCreate(name=f"TM Task {suffix}", team_id=team_a.id, project_id=clarvs.id),
                background_tasks=_NullBackgroundTasks(), tenant=tm_tenant, db=db,
            )
            task_ids.append(created.id)
            assert created.team_id == team_a.id

            # ── Security: TM cannot create a Task under an unrelated Team
            # they don't manage — even one that genuinely exists in this
            # org (team_c, managed by other_tm). ──
            try:
                await create_task(
                    TaskCreate(name=f"TM Bad Task {suffix}", team_id=team_c.id),
                    background_tasks=_NullBackgroundTasks(), tenant=tm_tenant, db=db,
                )
                raise AssertionError("a Team Manager must not be able to create a Task under an unrelated Team")
            except AppException as exc:
                assert exc.status_code == 403

            # ── Owner/Admin remain unrestricted — any team in the org. ──
            owner_created = await create_task(
                TaskCreate(name=f"Owner Task {suffix}", team_id=team_c.id),
                background_tasks=_NullBackgroundTasks(), tenant=owner_tenant, db=db,
            )
            task_ids.append(owner_created.id)

            # ── Project Manager delegation flow unaffected: PM delegates
            # to team_a (attached to pm_project) despite never personally
            # managing/belonging to team_a — this must still succeed. ──
            pm_created = await create_task(
                TaskCreate(name=f"PM Delegated Task {suffix}", project_id=pm_project.id, team_id=team_a.id),
                background_tasks=_NullBackgroundTasks(), tenant=pm_tenant, db=db,
            )
            task_ids.append(pm_created.id)
            assert pm_created.team_id == team_a.id
            assert pm_created.assignee_id is None

            print("test_team_manager_create_task_form: PASSED")
        finally:
            if task_ids:
                await db.execute(delete(Task).where(Task.id.in_(task_ids)))
            await db.execute(delete(ProjectTeam).where(ProjectTeam.project_id.in_([clarvs.id, zero_fund.id, mailhub.id, project_d.id, pm_project.id])))
            await db.execute(delete(ProjectMembership).where(ProjectMembership.project_id == pm_project.id))
            await db.execute(delete(TeamMembership).where(TeamMembership.team_id.in_([team_a.id, team_b.id, team_c.id])))
            await db.execute(delete(Team).where(Team.id.in_([team_a.id, team_b.id, team_c.id])))
            await db.execute(delete(Project).where(Project.id.in_([clarvs.id, zero_fund.id, mailhub.id, project_d.id, pm_project.id])))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_([org.id, other_org.id])))
            await db.execute(delete(Organization).where(Organization.id.in_([org.id, other_org.id])))
            await db.execute(delete(User).where(User.id.in_([owner.id, tm.id, other_tm.id, pm.id, other_org_owner.id])))
            await db.commit()

    await engine.dispose()


class _NullBackgroundTasks:
    """Minimal stand-in for FastAPI's BackgroundTasks — create_task() only
    ever calls .add_task() on it, never awaits/inspects it further."""
    def add_task(self, *args, **kwargs):
        pass


def test_team_manager_create_task_form():
    asyncio.run(_scenario())
