"""Regression tests for the Team Manager Task-scope / Working-Time
authorization follow-up:

  BUG #1 — a plain Team Manager's "All Tasks" (GET /tasks) returned every
  organization task instead of only tasks under the team(s) they manage.

  BUG #2 — GET /tasks/{id}/time rejected a Team Manager reading a task
  under their own managed team with "You can only view time tracked on
  your own tasks", because can_access_task() had no Team Manager branch
  at all (only Admin/Owner/assignee/PM+membership).

Covers app.api.routes.tasks.can_access_task's new Team Manager branch,
list_tasks' new scope_team_ids/scope_project_ids restriction, and
get_task_time_summaries' matching bulk Team Manager branch — all while
leaving Start/Stop strictly assignee-only (app.api.routes.tasks.
can_control_task_timer, unchanged by this follow-up).

Covers (see the spec's "BACKEND TESTS"):
  1. plain Team Manager All Tasks: managed Team Task included.
  2. managed Team #2 Task: included (same manager manages multiple teams).
  3. other Team's Task: excluded.
  4. team_id = NULL Task: excluded from Team Manager All Tasks.
  5. unrelated Project-only Task: excluded (plain Team Manager, no PM
     capability).
  6. Owner All Tasks: existing org-wide behavior unchanged.
  7. Admin All Tasks: unchanged.
  8. Project Manager scope: unchanged (still cannot reach GET /tasks at
     all — matches its pre-existing, already-established scope).
  9. combined Team Manager + Project Manager: All Tasks = managed-team
     tasks UNION project-member tasks — never the whole organization.
  10. Team Manager GET managed-Team Task: succeeds.
  11. Team Manager GET unrelated-Team Task: denied.
  12. Team Manager GET /tasks/{id}/time for managed-Team Task: succeeds
      even when not assignee.
  13. Working Time value is correct.
  14. Team Manager POST /time/start when not assignee: denied.
  15. Team Manager POST /time/stop when not assignee: denied.
  16. Team Manager who IS assignee: Start succeeds.
  17. same Team Manager: Stop succeeds.
  18. bulk /tasks/time-summaries: managed Team Task summary available.
  19. bulk summary: unrelated Team Task data does not leak.
  20. cross-tenant: still denied.
  21. Activity Log: unauthorized time read/control does not create fake
      timer events.
  22. Assignee eligibility tests remain passing — run as a full separate
      file (test_task_assignee_eligibility.py) in the regression pass,
      not duplicated here.

Runs against the real database connection the app uses. Every row this
test creates is deleted before it returns.
"""

import asyncio
import time
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete, select

from app.api.routes.tasks import get_task, get_task_time_state, get_task_time_summaries, list_tasks, start_task_timer, stop_task_timer
from app.core.activity_actions import ENTITY_TASK, TASK_TIMER_STARTED, TASK_TIMER_STOPPED
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import PROJECT_MANAGER, TEAM_MANAGER, TEAM_MEMBER
from app.core.tenant import TenantContext, require_org_manager
from app.models.activity_log import ActivityLog
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

        owner = User(full_name="TMS Owner", email=f"tms.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        admin = User(full_name="TMS Admin", email=f"tms.admin.{suffix}@test.invalid", hashed_password="x", role="admin")
        tm = User(full_name="TMS TM", email=f"tms.tm.{suffix}@test.invalid", hashed_password="x", role=TEAM_MANAGER)
        tm2 = User(full_name="TMS TM2", email=f"tms.tm2.{suffix}@test.invalid", hashed_password="x", role=TEAM_MANAGER)
        pm = User(full_name="TMS PM", email=f"tms.pm.{suffix}@test.invalid", hashed_password="x", role=PROJECT_MANAGER)
        alice = User(full_name="TMS Alice", email=f"tms.alice.{suffix}@test.invalid", hashed_password="x", role=TEAM_MEMBER)
        bob = User(full_name="TMS Bob", email=f"tms.bob.{suffix}@test.invalid", hashed_password="x", role=TEAM_MEMBER)
        outsider = User(full_name="TMS Outsider", email=f"tms.outsider.{suffix}@test.invalid", hashed_password="x", role="owner")
        db.add_all([owner, admin, tm, tm2, pm, alice, bob, outsider])
        await db.commit()
        for u in (owner, admin, tm, tm2, pm, alice, bob, outsider):
            await db.refresh(u)

        org = Organization(name=f"TMS Org {suffix}", slug=f"tms-org-{suffix}", owner_id=owner.id)
        other_org = Organization(name=f"TMS Other Org {suffix}", slug=f"tms-other-org-{suffix}", owner_id=outsider.id)
        db.add_all([org, other_org])
        await db.commit()
        for o in (org, other_org):
            await db.refresh(o)

        memberships = {}
        for u, role in [(owner, "owner"), (admin, "admin"), (tm, TEAM_MANAGER), (tm2, TEAM_MANAGER), (pm, PROJECT_MANAGER), (alice, TEAM_MEMBER), (bob, TEAM_MEMBER)]:
            m = OrganizationMembership(organization_id=org.id, user_id=u.id, role=role)
            db.add(m)
            memberships[u.id] = m
        outsider_membership = OrganizationMembership(organization_id=other_org.id, user_id=outsider.id, role="owner")
        db.add(outsider_membership)
        await db.commit()

        # tm manages TWO teams (A and B) — proves multi-team scoping.
        # tm2 manages team C, entirely unrelated to tm.
        team_a = Team(name=f"TMS Team A {suffix}", team_manager_id=tm.id, created_by_id=owner.id, organization_id=org.id)
        team_b = Team(name=f"TMS Team B {suffix}", team_manager_id=tm.id, created_by_id=owner.id, organization_id=org.id)
        team_c = Team(name=f"TMS Team C {suffix}", team_manager_id=tm2.id, created_by_id=owner.id, organization_id=org.id)
        db.add_all([team_a, team_b, team_c])
        await db.commit()
        for t in (team_a, team_b, team_c):
            await db.refresh(t)
        db.add_all([
            TeamMembership(team_id=team_a.id, user_id=tm.id),
            TeamMembership(team_id=team_a.id, user_id=alice.id),
            TeamMembership(team_id=team_b.id, user_id=tm.id),
            TeamMembership(team_id=team_b.id, user_id=bob.id),
            TeamMembership(team_id=team_c.id, user_id=tm2.id),
        ])
        await db.commit()

        project = Project(name=f"TMS Project {suffix}", created_by_id=owner.id, organization_id=org.id)
        db.add(project)
        await db.commit()
        await db.refresh(project)
        db.add(ProjectMembership(project_id=project.id, user_id=pm.id))
        await db.commit()

        task_a = Task(name=f"TMS Task A {suffix}", team_id=team_a.id, assignee_id=alice.id, created_by_id=owner.id, organization_id=org.id)
        task_b = Task(name=f"TMS Task B {suffix}", team_id=team_b.id, assignee_id=bob.id, created_by_id=owner.id, organization_id=org.id)
        task_c = Task(name=f"TMS Task C {suffix}", team_id=team_c.id, created_by_id=owner.id, organization_id=org.id)
        task_none = Task(name=f"TMS Task None {suffix}", created_by_id=owner.id, organization_id=org.id)
        task_project = Task(name=f"TMS Task Project {suffix}", project_id=project.id, created_by_id=owner.id, organization_id=org.id)
        # tm is a genuine, valid assignee of team_a (auto team-member as its manager).
        task_tm_assigned = Task(name=f"TMS Task TM Assigned {suffix}", team_id=team_a.id, assignee_id=tm.id, created_by_id=owner.id, organization_id=org.id)
        db.add_all([task_a, task_b, task_c, task_none, task_project, task_tm_assigned])
        await db.commit()
        for t in (task_a, task_b, task_c, task_none, task_project, task_tm_assigned):
            await db.refresh(t)

        task_ids_all = [task_a.id, task_b.id, task_c.id, task_none.id, task_project.id, task_tm_assigned.id]
        org_id, other_org_id = org.id, other_org.id
        team_ids_all = [team_a.id, team_b.id, team_c.id]
        project_id = project.id
        user_ids = [owner.id, admin.id, tm.id, tm2.id, pm.id, alice.id, bob.id, outsider.id]

        owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[owner.id], user=owner, db=db)
        admin_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[admin.id], user=admin, db=db)
        tm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[tm.id], user=tm, db=db)
        alice_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[alice.id], user=alice, db=db)
        outsider_tenant = TenantContext(organization_id=other_org.id, organization=other_org, membership=outsider_membership, user=outsider, db=db)

        try:
            # ── 1, 2, 3, 4, 5. Plain Team Manager All Tasks. ──────────────────
            tm_all = await list_tasks(status_filter=None, priority_filter=None, assignee_id=None, project_id=None, team_id=None, due_date_from=None, due_date_to=None, overdue=False, tenant=tm_tenant)
            tm_all_ids = {t.id for t in tm_all}
            assert task_a.id in tm_all_ids, "managed Team A's task must be included"
            assert task_b.id in tm_all_ids, "managed Team B's task must ALSO be included (multiple managed teams)"
            assert task_tm_assigned.id in tm_all_ids
            assert task_c.id not in tm_all_ids, "Team C is managed by tm2, not tm — must be excluded"
            assert task_none.id not in tm_all_ids, "a team-less task must never appear in Team Manager All Tasks"
            assert task_project.id not in tm_all_ids, "a project-only task must be excluded for a plain Team Manager (no PM capability)"

            # ── 6, 7. Owner/Admin All Tasks: unchanged, org-wide. ─────────────
            owner_all_ids = {t.id for t in await list_tasks(status_filter=None, priority_filter=None, assignee_id=None, project_id=None, team_id=None, due_date_from=None, due_date_to=None, overdue=False, tenant=owner_tenant)}
            for tid in task_ids_all:
                assert tid in owner_all_ids, "Owner must retain unrestricted org-wide All Tasks"
            admin_all_ids = {t.id for t in await list_tasks(status_filter=None, priority_filter=None, assignee_id=None, project_id=None, team_id=None, due_date_from=None, due_date_to=None, overdue=False, tenant=admin_tenant)}
            for tid in task_ids_all:
                assert tid in admin_all_ids, "Admin must retain unrestricted org-wide All Tasks"

            # ── 8. Project Manager scope unchanged: a plain PM (no Team
            # Manager capability) still cannot reach GET /tasks at all —
            # this was never part of PM's scope (PM lists via
            # GET /tasks/project/{id}), and this follow-up must not change
            # that either way. ─────────────────────────────────────────────────
            pm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[pm.id], user=pm, db=db)
            try:
                await require_org_manager(tenant=pm_tenant)
                raise AssertionError("a plain Project Manager must still not pass require_org_manager")
            except AppException as exc:
                assert exc.status_code == 403, exc

            # ── 9. Combined Team Manager + Project Manager: union of
            # managed-team tasks AND project-member tasks — never the whole
            # organization. Grant tm the PM privilege flag + project
            # membership without changing their functional role. ──────────────
            memberships[tm.id].is_project_manager = True
            db.add(ProjectMembership(project_id=project.id, user_id=tm.id))
            await db.commit()
            hybrid_all_ids = {t.id for t in await list_tasks(status_filter=None, priority_filter=None, assignee_id=None, project_id=None, team_id=None, due_date_from=None, due_date_to=None, overdue=False, tenant=tm_tenant)}
            assert task_a.id in hybrid_all_ids
            assert task_b.id in hybrid_all_ids
            assert task_project.id in hybrid_all_ids, "hybrid TM+PM must see project-member tasks too, via their PM capability"
            assert task_c.id not in hybrid_all_ids, "still never org-wide — Team C remains excluded"
            assert task_none.id not in hybrid_all_ids
            # revert for the rest of the scenario.
            memberships[tm.id].is_project_manager = False
            await db.execute(delete(ProjectMembership).where(ProjectMembership.project_id == project_id, ProjectMembership.user_id == tm.id))
            await db.commit()

            # ── 10, 11. Team Manager GET single task. ─────────────────────────
            got = await get_task(task_a.id, tenant=tm_tenant)
            assert got.id == task_a.id
            try:
                await get_task(task_c.id, tenant=tm_tenant)
                raise AssertionError("Team Manager must not be able to GET a task under an unrelated team")
            except Exception as exc:
                assert getattr(exc, "status_code", None) == 403, exc

            # ── 12, 13, 22. Team Manager GET /tasks/{id}/time for a managed
            # team task they are NOT the assignee of — must succeed (this is
            # the exact bug from the screenshot) and report the correct
            # Working Time total. ──────────────────────────────────────────────
            await start_task_timer(task_a.id, tenant=alice_tenant)
            time.sleep(1.1)
            await stop_task_timer(task_a.id, tenant=alice_tenant)
            tm_read = await get_task_time_state(task_a.id, tenant=tm_tenant)
            assert tm_read.tracked_time_seconds >= 1, "Working Time read must succeed and report the real total"
            assert tm_read.is_active is False
            assert tm_read.assignee_id == alice.id

            # ── 14, 15. Team Manager (not assignee) cannot Start/Stop. ────────
            try:
                await start_task_timer(task_a.id, tenant=tm_tenant)
                raise AssertionError("Team Manager must not be able to start a timer on a task assigned to someone else, even a task under their own managed team")
            except AppException as exc:
                assert exc.status_code == 403, exc
                assert exc.code == "TASK_TIMER_ASSIGNEE_ONLY", exc
            await start_task_timer(task_a.id, tenant=alice_tenant)  # alice starts again, for the Stop check below
            try:
                await stop_task_timer(task_a.id, tenant=tm_tenant)
                raise AssertionError("Team Manager must not be able to stop someone else's active timer")
            except AppException as exc:
                assert exc.status_code == 403, exc
                assert exc.code == "TASK_TIMER_ASSIGNEE_ONLY", exc
            await stop_task_timer(task_a.id, tenant=alice_tenant)

            # ── 16, 17. Team Manager who IS the assignee: Start/Stop succeed. ─
            tm_start = await start_task_timer(task_tm_assigned.id, tenant=tm_tenant)
            assert tm_start.is_active is True
            tm_stop = await stop_task_timer(task_tm_assigned.id, tenant=tm_tenant)
            assert tm_stop.is_active is False

            # ── 18, 19. Bulk summaries: managed-team task summary available;
            # unrelated-team task data does not leak. ─────────────────────────
            bulk = await get_task_time_summaries(TaskTimeSummariesRequest(task_ids=[task_a.id, task_c.id]), tenant=tm_tenant)
            assert str(task_a.id) in bulk.items, "a managed-team task's Working Time summary must be available to its Team Manager"
            assert str(task_c.id) not in bulk.items, "an unrelated team's task must never leak into the bulk summary"

            # ── 20. Cross-tenant: outsider still denied entirely. ─────────────
            try:
                await get_task_time_state(task_a.id, tenant=outsider_tenant)
                raise AssertionError("a user from a different organization must not reach this task at all")
            except AppException as exc:
                assert exc.status_code == 404, exc

            # ── 21. Activity Log: the denied Start/Stop attempts above must
            # NOT have created any timer_started/timer_stopped rows for tm. ──
            tm_log_rows = (await db.execute(
                select(ActivityLog).where(
                    ActivityLog.organization_id == org_id,
                    ActivityLog.entity_type == ENTITY_TASK,
                    ActivityLog.entity_id == task_a.id,
                    ActivityLog.actor_user_id == tm.id,
                    ActivityLog.action.in_([TASK_TIMER_STARTED, TASK_TIMER_STOPPED]),
                )
            )).scalars().all()
            assert tm_log_rows == [], "a denied Start/Stop attempt must never create an Activity Log timer event"

        finally:
            await db.execute(delete(TaskTimeEntry).where(TaskTimeEntry.task_id.in_(task_ids_all)))
            await db.execute(delete(ActivityLog).where(ActivityLog.organization_id.in_([org_id, other_org_id])))
            await db.execute(delete(Task).where(Task.id.in_(task_ids_all)))
            await db.execute(delete(TeamMembership).where(TeamMembership.team_id.in_(team_ids_all)))
            await db.execute(delete(Team).where(Team.id.in_(team_ids_all)))
            await db.execute(delete(ProjectMembership).where(ProjectMembership.project_id == project_id))
            await db.execute(delete(Project).where(Project.id == project_id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_([org_id, other_org_id])))
            await db.execute(delete(Organization).where(Organization.id.in_([org_id, other_org_id])))
            await db.execute(delete(User).where(User.id.in_(user_ids)))
            await db.commit()

    await engine.dispose()


def test_team_manager_task_scope():
    asyncio.run(_run())
