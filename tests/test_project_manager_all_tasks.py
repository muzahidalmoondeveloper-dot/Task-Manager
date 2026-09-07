"""Regression tests for the Project Manager "All Tasks" tab follow-up.

Previously a plain Project Manager could not reach `GET /tasks` at all
(`require_org_manager` admits only Owner/Admin/Team Manager) — their only
task-listing surface was `GET /tasks/project/{id}`, one project at a time,
with no combined view across every project they manage. This adds a new,
narrower `require_org_manager_or_project_manager` dependency (READ-only,
list endpoint only — task mutation routes are unchanged) and reuses the
Team-Manager-Task-scope follow-up's existing `scope_project_ids` query
logic in `TaskRepository.list_all()`, which already handled a Project
Manager's project scope correctly — it just could never be reached by a
plain PM before.

Covers (see the spec's "BACKEND TESTS"):
  1. plain Project Manager can now call GET /tasks (list_tasks) at all.
  2. PM manages Project A: Project A task included.
  3. PM manages Project B too: Project B task included (multi-project).
  4. Project C (PM not assigned): excluded.
  5. project_id = NULL task: excluded.
  6. team-only task outside PM's projects: excluded.
  7. unassigned task inside a managed project: included.
  8. task assigned to another user inside a managed project: included.
  9. task assigned to the PM themself inside a managed project: included.
  10. Owner All Tasks: unchanged, org-wide.
  11. Admin: unchanged.
  12. plain Team Manager: still managed-Team scope only (unaffected).
  13. hybrid Team Manager + Project Manager: managed-team tasks UNION
      project-member tasks — never the whole organization (established
      capability-union semantics, unchanged by this follow-up).
  14. cross-tenant ProjectMembership (same user, a project in a DIFFERENT
      org) does not authorize anything in the current organization.
  15. an unauthorized project_id query filter narrows to zero results,
      never expands scope.
  16. Status/Priority filters still apply inside the allowed scope.
  17. GET /tasks/{id} for a managed-project task: existing access
      preserved (unchanged — can_access_task already had this branch).
  18. Working-Time summary for a managed-project task: accessible to PM.
  19. Timer Start/Stop when PM is not the assignee: denied.
  20. Timer Start/Stop when PM is the assignee: works.
  21. Activity Log permissions remain unchanged (PM still cannot read
      GET /activity-logs).

Runs against the real database connection the app uses. Every row this
test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete

from app.api.routes.tasks import get_task, get_task_time_summaries, get_task_time_state, list_tasks, start_task_timer, stop_task_timer
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import PROJECT_MANAGER, TEAM_MANAGER, TEAM_MEMBER
from app.core.tenant import TenantContext, require_org_admin, require_org_manager_or_project_manager
from app.models.organization import Organization, OrganizationMembership
from app.models.project import Project, ProjectMembership
from app.models.task import Task
from app.models.task_time_entry import TaskTimeEntry
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.schemas.task_time_entry import TaskTimeSummariesRequest


async def _run():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]

        owner = User(full_name="PMAT Owner", email=f"pmat.owner.{suffix}@example-corp.com", hashed_password="x", role="owner")
        admin = User(full_name="PMAT Admin", email=f"pmat.admin.{suffix}@example-corp.com", hashed_password="x", role="admin")
        pm = User(full_name="PMAT PM", email=f"pmat.pm.{suffix}@example-corp.com", hashed_password="x", role=PROJECT_MANAGER)
        tm = User(full_name="PMAT TM", email=f"pmat.tm.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MANAGER)
        bob = User(full_name="PMAT Bob", email=f"pmat.bob.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)
        outsider = User(full_name="PMAT Outsider", email=f"pmat.outsider.{suffix}@example-corp.com", hashed_password="x", role="owner")
        db.add_all([owner, admin, pm, tm, bob, outsider])
        await db.commit()
        for u in (owner, admin, pm, tm, bob, outsider):
            await db.refresh(u)

        org = Organization(name=f"PMAT Org {suffix}", slug=f"pmat-org-{suffix}", owner_id=owner.id)
        other_org = Organization(name=f"PMAT Other Org {suffix}", slug=f"pmat-other-org-{suffix}", owner_id=outsider.id)
        db.add_all([org, other_org])
        await db.commit()
        for o in (org, other_org):
            await db.refresh(o)

        memberships = {}
        for u, role in [(owner, "owner"), (admin, "admin"), (pm, PROJECT_MANAGER), (tm, TEAM_MANAGER), (bob, TEAM_MEMBER)]:
            m = OrganizationMembership(organization_id=org.id, user_id=u.id, role=role)
            db.add(m)
            memberships[u.id] = m
        outsider_membership = OrganizationMembership(organization_id=other_org.id, user_id=outsider.id, role="owner")
        db.add(outsider_membership)
        await db.commit()

        # Projects A and B: PM manages both. Project C: PM has no membership.
        project_a = Project(name=f"PMAT Project A {suffix}", created_by_id=owner.id, organization_id=org.id)
        project_b = Project(name=f"PMAT Project B {suffix}", created_by_id=owner.id, organization_id=org.id)
        project_c = Project(name=f"PMAT Project C {suffix}", created_by_id=owner.id, organization_id=org.id)
        # A project in the OTHER organization — used for the cross-tenant
        # ProjectMembership probe below.
        other_org_project = Project(name=f"PMAT Other Org Project {suffix}", created_by_id=outsider.id, organization_id=other_org.id)
        db.add_all([project_a, project_b, project_c, other_org_project])
        await db.commit()
        for p in (project_a, project_b, project_c, other_org_project):
            await db.refresh(p)
        db.add_all([
            ProjectMembership(project_id=project_a.id, user_id=pm.id),
            ProjectMembership(project_id=project_b.id, user_id=pm.id),
            # Cross-tenant probe: pm ALSO holds a ProjectMembership row on a
            # project that belongs to a DIFFERENT organization entirely —
            # this must never authorize anything when pm is acting inside
            # `org` (its own TenantContext/org_id boundary).
            ProjectMembership(project_id=other_org_project.id, user_id=pm.id),
        ])
        await db.commit()

        team_x = Team(name=f"PMAT Team X {suffix}", team_manager_id=tm.id, created_by_id=owner.id, organization_id=org.id)
        db.add(team_x)
        await db.commit()
        await db.refresh(team_x)
        db.add(TeamMembership(team_id=team_x.id, user_id=tm.id))
        await db.commit()

        task_a_unassigned = Task(name=f"PMAT Task A1 {suffix}", project_id=project_a.id, created_by_id=owner.id, organization_id=org.id)
        task_a_other = Task(name=f"PMAT Task A2 {suffix}", project_id=project_a.id, assignee_id=bob.id, created_by_id=owner.id, organization_id=org.id)
        task_a_pm = Task(name=f"PMAT Task A3 {suffix}", project_id=project_a.id, assignee_id=pm.id, created_by_id=owner.id, organization_id=org.id, status="in_progress")
        task_b = Task(name=f"PMAT Task B1 {suffix}", project_id=project_b.id, created_by_id=owner.id, organization_id=org.id)
        task_c = Task(name=f"PMAT Task C1 {suffix}", project_id=project_c.id, created_by_id=owner.id, organization_id=org.id)
        task_none = Task(name=f"PMAT Task None {suffix}", created_by_id=owner.id, organization_id=org.id)
        task_team_only = Task(name=f"PMAT Task Team Only {suffix}", team_id=team_x.id, created_by_id=owner.id, organization_id=org.id)
        db.add_all([task_a_unassigned, task_a_other, task_a_pm, task_b, task_c, task_none, task_team_only])
        await db.commit()
        for t in (task_a_unassigned, task_a_other, task_a_pm, task_b, task_c, task_none, task_team_only):
            await db.refresh(t)

        all_task_ids = [task_a_unassigned.id, task_a_other.id, task_a_pm.id, task_b.id, task_c.id, task_none.id, task_team_only.id]
        project_ids = [project_a.id, project_b.id, project_c.id]
        team_ids = [team_x.id]
        user_ids = [owner.id, admin.id, pm.id, tm.id, bob.id, outsider.id]

        owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[owner.id], user=owner, db=db)
        admin_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[admin.id], user=admin, db=db)
        pm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[pm.id], user=pm, db=db)
        tm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[tm.id], user=tm, db=db)

        def _list(tenant, **overrides):
            kwargs = dict(
                status_filter=None, priority_filter=None, assignee_id=None,
                project_id=None, team_id=None, due_date_from=None, due_date_to=None,
                overdue=False, tenant=tenant,
            )
            kwargs.update(overrides)
            return list_tasks(**kwargs)

        try:
            # ── 1. Plain PM can now reach GET /tasks at all. ──────────────────
            await require_org_manager_or_project_manager(tenant=pm_tenant)  # must not raise

            # ── 2, 3, 4, 5, 6, 7, 8, 9. Scope correctness. ────────────────────
            pm_all = await _list(pm_tenant)
            pm_all_ids = {t.id for t in pm_all}
            assert task_a_unassigned.id in pm_all_ids, "unassigned task inside a managed project must be included"
            assert task_a_other.id in pm_all_ids, "task assigned to someone else inside a managed project must be included"
            assert task_a_pm.id in pm_all_ids, "task assigned to the PM themself inside a managed project must be included"
            assert task_b.id in pm_all_ids, "a SECOND managed project's task must also be included (multi-project support)"
            assert task_c.id not in pm_all_ids, "Project C (PM not assigned) must be excluded"
            assert task_none.id not in pm_all_ids, "a project-less task must never appear in PM All Tasks"
            assert task_team_only.id not in pm_all_ids, "a team-only task with no PM project relationship must be excluded"

            # ── 10, 11. Owner/Admin All Tasks unchanged, org-wide. ────────────
            owner_all_ids = {t.id for t in await _list(owner_tenant)}
            for tid in all_task_ids:
                assert tid in owner_all_ids, "Owner must retain unrestricted org-wide All Tasks"
            admin_all_ids = {t.id for t in await _list(admin_tenant)}
            for tid in all_task_ids:
                assert tid in admin_all_ids, "Admin must retain unrestricted org-wide All Tasks"

            # ── 12. Plain Team Manager: unaffected, still managed-Team scope
            # only (no project relationship here at all, so sees nothing). ────
            tm_all_ids = {t.id for t in await _list(tm_tenant)}
            assert task_team_only.id in tm_all_ids, "tm's own managed team's task must still appear"
            assert task_a_unassigned.id not in tm_all_ids, "a plain Team Manager must not see PM's project tasks"
            assert task_c.id not in tm_all_ids

            # ── 13. Hybrid Team Manager + Project Manager: union of
            # managed-team tasks AND project-member tasks — never the whole
            # organization (established capability-union semantics). ─────────
            memberships[tm.id].is_project_manager = True
            db.add(ProjectMembership(project_id=project_a.id, user_id=tm.id))
            await db.commit()
            hybrid_ids = {t.id for t in await _list(tm_tenant)}
            assert task_team_only.id in hybrid_ids, "still sees managed-team tasks"
            assert task_a_unassigned.id in hybrid_ids, "now ALSO sees project-member tasks via granted PM capability"
            assert task_c.id not in hybrid_ids, "still never org-wide"
            assert task_none.id not in hybrid_ids
            memberships[tm.id].is_project_manager = False
            await db.execute(delete(ProjectMembership).where(ProjectMembership.project_id == project_a.id, ProjectMembership.user_id == tm.id))
            await db.commit()

            # ── 14. Cross-tenant ProjectMembership does not authorize. The
            # `other_org_project` membership above must contribute nothing —
            # already implicitly proven (pm_all_ids has exactly the expected
            # set), but assert explicitly that no task from `other_org` ever
            # appears for a caller whose TenantContext.organization_id is
            # `org`. ────────────────────────────────────────────────────────
            assert not (pm_all_ids - {task_a_unassigned.id, task_a_other.id, task_a_pm.id, task_b.id}), (
                "no unexpected task leaked in — including nothing derived from the other-org ProjectMembership"
            )

            # ── 15. Unauthorized project_id filter narrows to zero, never
            # expands scope. ────────────────────────────────────────────────
            unauthorized_filtered = await _list(pm_tenant, project_id=project_c.id)
            assert unauthorized_filtered == [], "filtering by a project the PM does not manage must return nothing, never expand scope"

            # Authorized project filter narrows correctly WITHIN scope.
            authorized_filtered = {t.id for t in await _list(pm_tenant, project_id=project_a.id)}
            assert authorized_filtered == {task_a_unassigned.id, task_a_other.id, task_a_pm.id}

            # ── 16. Status/Priority filters still apply inside scope. ────────
            status_filtered = {t.id for t in await _list(pm_tenant, status_filter="in_progress")}
            assert status_filtered == {task_a_pm.id}

            # ── 17. GET /tasks/{id} for a managed-project task: preserved. ───
            got = await get_task(task_a_other.id, tenant=pm_tenant)
            assert got.id == task_a_other.id
            try:
                await get_task(task_c.id, tenant=pm_tenant)
                raise AssertionError("PM must not be able to GET a task under an unassigned project")
            except Exception as exc:
                assert getattr(exc, "status_code", None) == 403, exc

            # ── 18. Working-Time summary accessible for a managed-project
            # task PM is not the assignee of. ─────────────────────────────────
            bulk = await get_task_time_summaries(TaskTimeSummariesRequest(task_ids=[task_a_other.id, task_c.id]), tenant=pm_tenant)
            assert str(task_a_other.id) in bulk.items, "Working Time summary must be available for a managed-project task"
            assert str(task_c.id) not in bulk.items, "an unrelated project's task must never leak into the bulk summary"

            time_state = await get_task_time_state(task_a_other.id, tenant=pm_tenant)
            assert time_state.assignee_id == bob.id

            # ── 19, 20. Timer control remains assignee-only. ─────────────────
            try:
                await start_task_timer(task_a_other.id, tenant=pm_tenant)
                raise AssertionError("PM must not be able to start a timer on a task assigned to someone else")
            except AppException as exc:
                assert exc.status_code == 403, exc
                assert exc.code == "TASK_TIMER_ASSIGNEE_ONLY", exc

            pm_start = await start_task_timer(task_a_pm.id, tenant=pm_tenant)
            assert pm_start.is_active is True
            pm_stop = await stop_task_timer(task_a_pm.id, tenant=pm_tenant)
            assert pm_stop.is_active is False

            # ── 21. Activity Log permissions unchanged: PM still denied. ─────
            try:
                await require_org_admin(tenant=pm_tenant)
                raise AssertionError("a plain Project Manager must still not be able to read the org-wide Activity Log")
            except AppException as exc:
                assert exc.status_code == 403, exc

        finally:
            await db.execute(delete(TaskTimeEntry).where(TaskTimeEntry.task_id.in_(all_task_ids)))
            await db.execute(delete(Task).where(Task.id.in_(all_task_ids)))
            await db.execute(delete(TeamMembership).where(TeamMembership.team_id.in_(team_ids)))
            await db.execute(delete(Team).where(Team.id.in_(team_ids)))
            await db.execute(delete(ProjectMembership).where(ProjectMembership.project_id.in_(project_ids + [other_org_project.id])))
            await db.execute(delete(Project).where(Project.id.in_(project_ids + [other_org_project.id])))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_([org.id, other_org.id])))
            await db.execute(delete(Organization).where(Organization.id.in_([org.id, other_org.id])))
            await db.execute(delete(User).where(User.id.in_(user_ids)))
            await db.commit()

    await engine.dispose()


def test_project_manager_all_tasks():
    asyncio.run(_run())
