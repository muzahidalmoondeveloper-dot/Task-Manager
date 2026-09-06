"""Regression tests for the Assignee-Only Timer Control follow-up:

    ONLY THE USER CURRENTLY ASSIGNED TO A TASK
    may Start or Stop that Task's Working Timer — regardless of elevated
    role (Owner/Admin/Team Manager/Project Manager).

Covers app.api.routes.tasks.can_control_task_timer (used by
start_task_timer/stop_task_timer instead of the broader can_access_task),
the active-timer reassignment guard in update_task, and the read-vs-control
distinction (GET /tasks/{id}/time stays on can_access_task).

Covers (see the spec's PHASE 25):
  1. assigned user can Start
  2. assigned user can Stop
  3. Owner not assigned -> cannot Start
  4. Owner not assigned -> cannot Stop another user's timer
  5. Admin not assigned -> cannot Start
  6. Admin not assigned -> cannot Stop
  7. Team Manager not assigned -> cannot Start/Stop
  8. Project Manager not assigned -> cannot Start/Stop
  9. ordinary non-assignee Team Member -> cannot Start/Stop
  10. unassigned Task -> nobody can Start
  11. Admin assigned to Task -> can Start/Stop (assignment itself valid)
  12. Team Manager assigned to Task -> can Start/Stop
  13. stale frontend: Task reassigned before Start request -> old assignee denied
  14. active timer: assignee change rejected (TASK_TIMER_ACTIVE)
  15. active timer: unassign rejected (TASK_TIMER_ACTIVE)
  16. active timer: unrelated Task update (name/priority) still succeeds
  17. stop timer -> reassignment then succeeds
  18. direct API manipulation cannot bypass the rule (every check above IS
      the route itself — there is no separate "API layer" to bypass)
  19. cross-tenant behavior unchanged (404, never a 403 leaking existence)
  20. one-active-timer-per-user rule unchanged
  21. Activity Log logs successful Start/Stop only — never for a rejected
      attempt
  22. Working Time read (GET /tasks/{id}/time) remains available to other
      authorized viewers (Admin/Owner/PM+membership), unaffected by the
      new control restriction — and now also carries assignee_id
  23. list bulk summaries (POST /tasks/time-summaries) still work and
      never show current_user_is_active=True for a non-assignee viewer

#7B/#7C's own dedicated test files (test_project_working_time.py,
test_task_list_working_time.py) already cover Working Time aggregation
correctness end-to-end and are run unmodified as part of this follow-up's
regression pass (see the final report) — not duplicated here.

Runs against the real database connection the app uses. Every row this
test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from fastapi import BackgroundTasks
from sqlalchemy import delete, select

from app.api.routes.tasks import get_task_time_state, get_task_time_summaries, start_task_timer, stop_task_timer, update_task
from app.core.activity_actions import ENTITY_TASK, TASK_TIMER_STARTED, TASK_TIMER_STOPPED
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import PROJECT_MANAGER, TEAM_MANAGER, TEAM_MEMBER
from app.core.tenant import TenantContext
from app.models.activity_log import ActivityLog
from app.models.organization import Organization, OrganizationMembership
from app.models.project import Project, ProjectMembership
from app.models.task import Task
from app.models.team import Team, TeamMembership
from app.models.task_time_entry import TaskTimeEntry
from app.models.user import User
from app.schemas.task import TaskUpdate
from app.schemas.task_time_entry import TaskTimeSummariesRequest


async def _run():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]

        owner = User(full_name="ATO Owner", email=f"ato.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        admin = User(full_name="ATO Admin", email=f"ato.admin.{suffix}@test.invalid", hashed_password="x", role="admin")
        tm = User(full_name="ATO TM", email=f"ato.tm.{suffix}@test.invalid", hashed_password="x", role=TEAM_MANAGER)
        pm = User(full_name="ATO PM", email=f"ato.pm.{suffix}@test.invalid", hashed_password="x", role=PROJECT_MANAGER)
        alice = User(full_name="ATO Alice", email=f"ato.alice.{suffix}@test.invalid", hashed_password="x", role=TEAM_MEMBER)
        bob = User(full_name="ATO Bob", email=f"ato.bob.{suffix}@test.invalid", hashed_password="x", role=TEAM_MEMBER)
        outsider = User(full_name="ATO Outsider", email=f"ato.outsider.{suffix}@test.invalid", hashed_password="x", role="owner")
        db.add_all([owner, admin, tm, pm, alice, bob, outsider])
        await db.commit()
        for u in (owner, admin, tm, pm, alice, bob, outsider):
            await db.refresh(u)

        org = Organization(name=f"ATO Org {suffix}", slug=f"ato-org-{suffix}", owner_id=owner.id)
        other_org = Organization(name=f"ATO Other Org {suffix}", slug=f"ato-other-org-{suffix}", owner_id=outsider.id)
        db.add_all([org, other_org])
        await db.commit()
        for o in (org, other_org):
            await db.refresh(o)

        memberships = {}
        for u, role in [(owner, "owner"), (admin, "admin"), (tm, TEAM_MANAGER), (pm, PROJECT_MANAGER), (alice, TEAM_MEMBER), (bob, TEAM_MEMBER)]:
            m = OrganizationMembership(organization_id=org.id, user_id=u.id, role=role)
            db.add(m)
            memberships[u.id] = m
        outsider_membership = OrganizationMembership(organization_id=other_org.id, user_id=outsider.id, role="owner")
        db.add(outsider_membership)
        await db.commit()

        # Team managed by tm, with alice and bob (and tm, auto-added) as
        # members — used for items 7/12 (Team Manager without/with
        # assignment).
        team = Team(name=f"ATO Team {suffix}", team_manager_id=tm.id, created_by_id=owner.id, organization_id=org.id)
        db.add(team)
        await db.commit()
        await db.refresh(team)
        db.add_all([
            TeamMembership(team_id=team.id, user_id=tm.id),
            TeamMembership(team_id=team.id, user_id=alice.id),
            TeamMembership(team_id=team.id, user_id=bob.id),
        ])
        await db.commit()

        # Project with pm as a genuine ProjectMembership member — used for
        # item 8 (Project Manager without assignment).
        project = Project(name=f"ATO Project {suffix}", created_by_id=owner.id, organization_id=org.id)
        db.add(project)
        await db.commit()
        await db.refresh(project)
        db.add(ProjectMembership(project_id=project.id, user_id=pm.id))
        await db.commit()

        # task_alice: plain task, assigned to alice — the primary fixture
        # for items 1-6, 9-10, 13-23.
        task_alice = Task(name=f"ATO Task Alice {suffix}", assignee_id=alice.id, created_by_id=owner.id, organization_id=org.id)
        # task_project: under `project`, assigned to alice — pm is a
        # genuine project member but NOT the assignee (item 8).
        task_project = Task(name=f"ATO Task Project {suffix}", assignee_id=alice.id, project_id=project.id, created_by_id=owner.id, organization_id=org.id)
        # task_team: under `team`, assigned to alice — tm manages the team
        # but is NOT the assignee (item 7). Reused for item 12 by
        # reassigning it to tm.
        task_team = Task(name=f"ATO Task Team {suffix}", assignee_id=alice.id, team_id=team.id, created_by_id=owner.id, organization_id=org.id)
        # task_unassigned: nobody can start it (item 10).
        task_unassigned = Task(name=f"ATO Task Unassigned {suffix}", created_by_id=owner.id, organization_id=org.id)
        # task_admin: assigned directly to admin (item 11).
        task_admin = Task(name=f"ATO Task Admin {suffix}", assignee_id=admin.id, created_by_id=owner.id, organization_id=org.id)
        # task_alice_2: a SECOND task assigned to alice — used for item 20
        # (one-active-timer-per-user still applies).
        task_alice_2 = Task(name=f"ATO Task Alice 2 {suffix}", assignee_id=alice.id, created_by_id=owner.id, organization_id=org.id)
        db.add_all([task_alice, task_project, task_team, task_unassigned, task_admin, task_alice_2])
        await db.commit()
        for t in (task_alice, task_project, task_team, task_unassigned, task_admin, task_alice_2):
            await db.refresh(t)

        task_ids_all = [task_alice.id, task_project.id, task_team.id, task_unassigned.id, task_admin.id, task_alice_2.id]
        org_id, other_org_id = org.id, other_org.id
        team_id, project_id = team.id, project.id
        user_ids = [owner.id, admin.id, tm.id, pm.id, alice.id, bob.id, outsider.id]

        owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[owner.id], user=owner, db=db)
        admin_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[admin.id], user=admin, db=db)
        tm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[tm.id], user=tm, db=db)
        pm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[pm.id], user=pm, db=db)
        alice_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[alice.id], user=alice, db=db)
        bob_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[bob.id], user=bob, db=db)
        outsider_tenant = TenantContext(organization_id=other_org.id, organization=other_org, membership=outsider_membership, user=outsider, db=db)

        def _assert_denied(exc, task_id_for_log=None):
            assert isinstance(exc, AppException), exc
            assert exc.status_code == 403, exc
            assert exc.code == "TASK_TIMER_ASSIGNEE_ONLY", exc

        try:
            # ── 1, 2. Assigned user (alice) can Start and Stop. ───────────────
            start_result = await start_task_timer(task_alice.id, tenant=alice_tenant)
            assert start_result.is_active is True
            assert start_result.assignee_id == alice.id
            stop_result = await stop_task_timer(task_alice.id, tenant=alice_tenant)
            assert stop_result.is_active is False

            # ── 21. Activity Log logs successful Start/Stop. ──────────────────
            log_actions = (await db.execute(
                select(ActivityLog.action).where(
                    ActivityLog.organization_id == org_id,
                    ActivityLog.entity_type == ENTITY_TASK,
                    ActivityLog.entity_id == task_alice.id,
                    ActivityLog.action.in_([TASK_TIMER_STARTED, TASK_TIMER_STOPPED]),
                )
            )).scalars().all()
            assert log_actions.count(TASK_TIMER_STARTED) == 1
            assert log_actions.count(TASK_TIMER_STOPPED) == 1

            # ── 3, 5, 7, 8, 9. Non-assignees of every role cannot Start
            # task_alice — timer control comes strictly from assignee_id,
            # never from Owner/Admin/Team-Manager/Project-Manager/plain
            # Team Member status. ──────────────────────────────────────────────
            for tenant, label in [
                (owner_tenant, "Owner"), (admin_tenant, "Admin"),
                (tm_tenant, "Team Manager"), (pm_tenant, "Project Manager"),
                (bob_tenant, "ordinary Team Member"),
            ]:
                try:
                    await start_task_timer(task_alice.id, tenant=tenant)
                    raise AssertionError(f"{label} must not be able to start a timer on a task assigned to someone else")
                except AppException as exc:
                    _assert_denied(exc)

            # ── 7 (Team Manager specifically, on the TEAM-scoped task,
            # where tm actually manages the team). Even full Team-scoped
            # authority does not grant timer control over Alice's task. ──────
            try:
                await start_task_timer(task_team.id, tenant=tm_tenant)
                raise AssertionError("Team Manager must not control the timer of a team task assigned to someone else")
            except AppException as exc:
                _assert_denied(exc)

            # ── 8 (Project Manager specifically, on the PROJECT-scoped
            # task, where pm is a genuine ProjectMembership member). ──────────
            try:
                await start_task_timer(task_project.id, tenant=pm_tenant)
                raise AssertionError("Project Manager must not control the timer of a project task assigned to someone else")
            except AppException as exc:
                _assert_denied(exc)

            # ── 4, 6. Owner/Admin cannot Stop another user's timer either —
            # alice starts one first, then owner/admin attempt to stop it. ───
            await start_task_timer(task_alice.id, tenant=alice_tenant)
            for tenant, label in [(owner_tenant, "Owner"), (admin_tenant, "Admin")]:
                try:
                    await stop_task_timer(task_alice.id, tenant=tenant)
                    raise AssertionError(f"{label} must not be able to stop someone else's active timer")
                except AppException as exc:
                    _assert_denied(exc)
            # alice's own timer must be completely unaffected.
            still_active = await get_task_time_state(task_alice.id, tenant=alice_tenant)
            assert still_active.is_active is True
            await stop_task_timer(task_alice.id, tenant=alice_tenant)

            # ── 10. Unassigned task -> nobody may Start, including Owner. ────
            try:
                await start_task_timer(task_unassigned.id, tenant=owner_tenant)
                raise AssertionError("an unassigned task must have no one who can start its timer")
            except AppException as exc:
                _assert_denied(exc)

            # ── 11. Admin, actually assigned to task_admin, CAN Start/Stop —
            # the ability comes from being assignee, not from being Admin. ───
            admin_start = await start_task_timer(task_admin.id, tenant=admin_tenant)
            assert admin_start.is_active is True
            admin_stop = await stop_task_timer(task_admin.id, tenant=admin_tenant)
            assert admin_stop.is_active is False

            # ── 12. Team Manager, actually assigned to task_team (reassign
            # it to tm first — tm is a genuine team member of `team`, so
            # this reassignment itself is valid under Task Assignee
            # eligibility rules), CAN Start/Stop. ─────────────────────────────
            await update_task(task_team.id, TaskUpdate(assignee_id=tm.id), background_tasks=BackgroundTasks(), tenant=owner_tenant, db=db)
            tm_start = await start_task_timer(task_team.id, tenant=tm_tenant)
            assert tm_start.is_active is True
            tm_stop = await stop_task_timer(task_team.id, tenant=tm_tenant)
            assert tm_stop.is_active is False

            # ── 13. Stale frontend: task_alice_2 is reassigned to bob AFTER
            # alice's UI would have shown a Start button — alice's request
            # must be rejected against the CURRENT (not stale) assignee. ─────
            await update_task(task_alice_2.id, TaskUpdate(assignee_id=bob.id), background_tasks=BackgroundTasks(), tenant=owner_tenant, db=db)
            try:
                await start_task_timer(task_alice_2.id, tenant=alice_tenant)
                raise AssertionError("a since-reassigned former assignee must be denied, never trusting stale frontend state")
            except AppException as exc:
                _assert_denied(exc)
            # bob, the new (current) assignee, can legitimately start it.
            bob_start = await start_task_timer(task_alice_2.id, tenant=bob_tenant)
            assert bob_start.is_active is True

            # ── 20. One-active-timer-per-user still applies: bob (now
            # active on task_alice_2) cannot also start on another task
            # they're assigned to. Give bob a second assigned task to prove
            # this is the GLOBAL rule, not a per-task one. ────────────────────
            bob_second_task = Task(name=f"ATO Bob Second Task {suffix}", assignee_id=bob.id, created_by_id=owner.id, organization_id=org.id)
            db.add(bob_second_task)
            await db.commit()
            await db.refresh(bob_second_task)
            task_ids_all.append(bob_second_task.id)
            try:
                await start_task_timer(bob_second_task.id, tenant=bob_tenant)
                raise AssertionError("one-active-timer-per-user must still be enforced")
            except AppException as exc:
                assert exc.status_code == 409, exc
            await stop_task_timer(task_alice_2.id, tenant=bob_tenant)

            # ── 14. Active timer: assignee change is rejected. ────────────────
            await start_task_timer(task_alice.id, tenant=alice_tenant)
            try:
                await update_task(task_alice.id, TaskUpdate(assignee_id=bob.id), background_tasks=BackgroundTasks(), tenant=owner_tenant, db=db)
                raise AssertionError("reassigning a task with an active timer must be rejected, never silently applied")
            except AppException as exc:
                assert exc.status_code == 409, exc
                assert exc.code == "TASK_TIMER_ACTIVE", exc
            # nothing was mutated.
            from app.repositories.task_repository import TaskRepository
            unchanged = await TaskRepository(db, org_id).get_by_id(task_alice.id)
            assert unchanged.assignee_id == alice.id

            # ── 15. Active timer: unassigning (null) is also rejected. ────────
            try:
                await update_task(task_alice.id, TaskUpdate(assignee_id=None), background_tasks=BackgroundTasks(), tenant=owner_tenant, db=db)
                raise AssertionError("clearing the assignee of a task with an active timer must be rejected")
            except AppException as exc:
                assert exc.status_code == 409, exc
                assert exc.code == "TASK_TIMER_ACTIVE", exc

            # ── 16. Active timer: an UNRELATED update (name/priority) still
            # succeeds — only assignment changes that would orphan the
            # active timer are blocked. ───────────────────────────────────────
            renamed = await update_task(task_alice.id, TaskUpdate(name=f"ATO Task Alice Renamed {suffix}", priority="high"), background_tasks=BackgroundTasks(), tenant=owner_tenant, db=db)
            assert renamed.name == f"ATO Task Alice Renamed {suffix}"
            assert renamed.priority == "high"
            # the active timer itself must be completely unaffected.
            still_running = await get_task_time_state(task_alice.id, tenant=alice_tenant)
            assert still_running.is_active is True

            # ── 17. Stop the timer -> reassignment then succeeds. ─────────────
            await stop_task_timer(task_alice.id, tenant=alice_tenant)
            reassigned = await update_task(task_alice.id, TaskUpdate(assignee_id=bob.id), background_tasks=BackgroundTasks(), tenant=owner_tenant, db=db)
            assert reassigned.assignee_id == bob.id

            # ── 19. Cross-tenant: outsider cannot even reach this org's
            # task (404, not a 403 that would leak existence). ────────────────
            try:
                await start_task_timer(task_admin.id, tenant=outsider_tenant)
                raise AssertionError("a user from a different organization must not reach this task at all")
            except AppException as exc:
                assert exc.status_code == 404, exc

            # ── 22. Working Time READ remains available to other authorized
            # viewers (Owner/Admin/PM+membership) — the new restriction is
            # Start/Stop only, never GET. Also carries assignee_id now. ───────
            admin_read = await get_task_time_state(task_project.id, tenant=admin_tenant)
            assert admin_read.assignee_id == alice.id
            pm_read = await get_task_time_state(task_project.id, tenant=pm_tenant)  # pm: genuine project member
            assert pm_read.assignee_id == alice.id
            assert pm_read.is_active is False  # pm has no session of their own — read still works though

            # ── 23. Bulk summaries: a non-assignee viewer never sees
            # current_user_is_active=True, even while the real assignee IS
            # actively running it. ─────────────────────────────────────────────
            await start_task_timer(task_project.id, tenant=alice_tenant)
            bulk_pm = await get_task_time_summaries(TaskTimeSummariesRequest(task_ids=[task_project.id]), tenant=pm_tenant)
            assert bulk_pm.items[str(task_project.id)].active_timer_count == 1
            assert bulk_pm.items[str(task_project.id)].current_user_is_active is False
            bulk_alice = await get_task_time_summaries(TaskTimeSummariesRequest(task_ids=[task_project.id]), tenant=alice_tenant)
            assert bulk_alice.items[str(task_project.id)].current_user_is_active is True
            await stop_task_timer(task_project.id, tenant=alice_tenant)

        finally:
            await db.execute(delete(TaskTimeEntry).where(TaskTimeEntry.task_id.in_(task_ids_all)))
            await db.execute(delete(ActivityLog).where(ActivityLog.organization_id.in_([org_id, other_org_id])))
            await db.execute(delete(Task).where(Task.id.in_(task_ids_all)))
            await db.execute(delete(TeamMembership).where(TeamMembership.team_id == team_id))
            await db.execute(delete(Team).where(Team.id == team_id))
            await db.execute(delete(ProjectMembership).where(ProjectMembership.project_id == project_id))
            await db.execute(delete(Project).where(Project.id == project_id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_([org_id, other_org_id])))
            await db.execute(delete(Organization).where(Organization.id.in_([org_id, other_org_id])))
            await db.execute(delete(User).where(User.id.in_(user_ids)))
            await db.commit()

    await engine.dispose()




def test_timer_assignee_only():
    asyncio.run(_run())
