"""Regression tests for Task #7A — the task time-tracking foundation
(app.models.task_time_entry.TaskTimeEntry / app.repositories.
task_time_entry_repository / POST /tasks/{id}/time/start|stop, GET
/tasks/{id}/time).

Covers (see PHASE 25 of the spec):
  1-4. authorized start -> active state persists -> stop -> duration correct
  5. accumulated total across multiple sessions on the same task
  6. duplicate active timer for the same user is rejected (same task)
  7. a second timer on a different task is rejected while one is active
     (one-active-timer-per-user, not per-task)
  8. stopping with no active timer returns a clear error, not a crash
  9. a user with no timer-control right (not this task's assignee) can
     never Stop someone else's timer — there is no entry_id parameter at
     all, so the only possible target is "my own session on this task",
     but Assignee-Only Timer Control (see test_timer_assignee_only.py for
     the dedicated suite) now rejects even reaching that check unless the
     caller IS the current assignee.
  10. cross-tenant task access is rejected (org-scoped get_task_or_404)
  11. a task with no entries returns zero, not null/NaN
  12. duration is computed server-side (elapsed >= a known sleep, not
      client-influenced) — see note below on why this isn't literally a
      >24h test (that would make the suite slow); the arithmetic itself
      (stopped_at - started_at, floored at 0) is exercised directly here.
  13. a Project Manager who is genuinely the task's assignee can
      start/stop — NOT merely via project membership (Assignee-Only Timer
      Control follow-up superseded the old "can_access_task is enough"
      rule; project membership alone no longer grants timer control, see
      item 13b below and test_timer_assignee_only.py for the full matrix).
  13b. that same Project Manager, NOT the assignee of a different task in
      that same project, is rejected from timer control on it despite
      having full read/manage access to it.
  14. a Team Manager with no relation to the task at all is rejected —
      unchanged.
  15. Admin/Owner have NO timer-control override — Owner is rejected from
      a task assigned to someone else, and only gains control by actually
      becoming its assignee (Phase 8 of the Assignee-Only Timer Control
      spec: "the ability comes from being assignee, not being Admin").
  16. concurrent Start requests for the same user are serialized by the
      DB's partial unique index — only one succeeds

See tests/test_timer_assignee_only.py for the full, dedicated
Assignee-Only Timer Control authorization matrix (Owner/Admin/Team
Manager/Project Manager without assignment, stale-frontend races,
active-timer reassignment protection, etc.) — this file stays focused on
#7A's original foundation, updated only where the new rule directly
contradicts what it used to assert.

Runs against the real database connection the app uses. Every row this
test creates is deleted before it returns.
"""

import asyncio
import time
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete

from app.api.routes.tasks import get_task_time_state, start_task_timer, stop_task_timer
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import PROJECT_MANAGER, TEAM_MANAGER, TEAM_MEMBER
from app.core.tenant import TenantContext
from app.models.organization import Organization, OrganizationMembership
from app.models.project import Project, ProjectMembership
from app.models.task import Task
from app.models.task_time_entry import TaskTimeEntry
from app.models.user import User
from app.repositories.task_time_entry_repository import DuplicateActiveTimerError, TaskTimeEntryRepository


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]

        owner = User(full_name="Timer Owner", email=f"timer.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        worker = User(full_name="Timer Worker", email=f"timer.worker.{suffix}@test.invalid", hashed_password="x", role="team_member")
        other_member = User(full_name="Timer Other Member", email=f"timer.other.{suffix}@test.invalid", hashed_password="x", role="team_member")
        pm = User(full_name="Timer PM", email=f"timer.pm.{suffix}@test.invalid", hashed_password="x", role=PROJECT_MANAGER)
        tm = User(full_name="Timer TM", email=f"timer.tm.{suffix}@test.invalid", hashed_password="x", role=TEAM_MANAGER)
        outsider = User(full_name="Timer Outsider", email=f"timer.outsider.{suffix}@test.invalid", hashed_password="x", role="owner")
        db.add_all([owner, worker, other_member, pm, tm, outsider])
        await db.commit()
        for u in (owner, worker, other_member, pm, tm, outsider):
            await db.refresh(u)

        org = Organization(name=f"Timer Org {suffix}", slug=f"timer-org-{suffix}", owner_id=owner.id)
        other_org = Organization(name=f"Timer Other Org {suffix}", slug=f"timer-other-org-{suffix}", owner_id=outsider.id)
        db.add_all([org, other_org])
        await db.commit()
        for o in (org, other_org):
            await db.refresh(o)

        memberships = {}
        for u, role in [(owner, "owner"), (worker, TEAM_MEMBER), (other_member, TEAM_MEMBER), (pm, PROJECT_MANAGER), (tm, TEAM_MANAGER)]:
            m = OrganizationMembership(organization_id=org.id, user_id=u.id, role=role)
            db.add(m)
            memberships[u.id] = m
        outsider_membership = OrganizationMembership(organization_id=other_org.id, user_id=outsider.id, role="owner")
        db.add(outsider_membership)
        await db.commit()
        for m in list(memberships.values()) + [outsider_membership]:
            await db.refresh(m)

        project = Project(name=f"Timer Project {suffix}", created_by_id=owner.id, organization_id=org.id)
        db.add(project)
        await db.commit()
        await db.refresh(project)
        db.add(ProjectMembership(project_id=project.id, user_id=pm.id))
        await db.commit()

        task = Task(name=f"Timer Task {suffix}", assignee_id=worker.id, project_id=project.id, created_by_id=owner.id, organization_id=org.id)
        other_task = Task(name=f"Other Timer Task {suffix}", assignee_id=worker.id, created_by_id=owner.id, organization_id=org.id)
        outsider_task = Task(name=f"Outsider Task {suffix}", created_by_id=outsider.id, organization_id=other_org.id)
        # A second project task, assigned directly to pm — used to prove
        # Assignee-Only Timer Control: pm may control THIS one (they're its
        # assignee) but not `task` above (assigned to worker, pm is merely
        # a project member of the same project).
        pm_task = Task(name=f"PM Timer Task {suffix}", assignee_id=pm.id, project_id=project.id, created_by_id=owner.id, organization_id=org.id)
        db.add_all([task, other_task, outsider_task, pm_task])
        await db.commit()
        for t in (task, other_task, outsider_task, pm_task):
            await db.refresh(t)
        # Captured as plain ints now, before any further commits in this
        # session expire these ORM objects — cleanup below must not rely on
        # lazy-loading `.id` off a possibly-expired instance (that lazy
        # load itself needs an active greenlet context, which isn't always
        # available in a bare `finally` block after several commits/
        # rollbacks earlier in the same session).
        task_id, other_task_id, outsider_task_id, pm_task_id = task.id, other_task.id, outsider_task.id, pm_task.id
        project_id, org_id, other_org_id = project.id, org.id, other_org.id
        user_ids = [owner.id, worker.id, other_member.id, pm.id, tm.id, outsider.id]

        worker_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[worker.id], user=worker, db=db)
        pm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[pm.id], user=pm, db=db)
        tm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[tm.id], user=tm, db=db)
        owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[owner.id], user=owner, db=db)
        outsider_tenant = TenantContext(organization_id=other_org.id, organization=other_org, membership=outsider_membership, user=outsider, db=db)

        created_entry_ids = []
        try:
            # ── 11. Task with no entries returns zero. ──────────────────────
            zero_state = await get_task_time_state(task.id, tenant=worker_tenant)
            assert zero_state.tracked_time_seconds == 0
            assert zero_state.is_active is False
            assert zero_state.active_started_at is None

            # ── 14. A Team Manager who is not the assignee, not admin, and
            # not a project member (has_project_manager_access is False for
            # a plain Team Manager) cannot even reach the task. ─────────────
            try:
                await start_task_timer(task.id, tenant=tm_tenant)
                raise AssertionError("a plain Team Manager with no relation to this task must not be able to start its timer")
            except Exception as exc:
                assert getattr(exc, "status_code", None) == 403, exc

            # ── 1-3. Assignee starts a timer -> active state persists. ──────
            start_result = await start_task_timer(task.id, tenant=worker_tenant)
            assert start_result.is_active is True
            assert start_result.active_started_at is not None
            assert start_result.tracked_time_seconds >= 0

            state_after_start = await get_task_time_state(task.id, tenant=worker_tenant)
            assert state_after_start.is_active is True, "active state must be readable independently (simulates a page reload)"

            # ── 6. Starting again on the same task is rejected. ─────────────
            try:
                await start_task_timer(task.id, tenant=worker_tenant)
                raise AssertionError("a second Start on the same task while one is active must be rejected")
            except AppException as exc:
                assert exc.status_code == 409, exc

            # ── 7. Starting a DIFFERENT task while one is active is also
            # rejected — one active timer per user, not per task. ───────────
            try:
                await start_task_timer(other_task.id, tenant=worker_tenant)
                raise AssertionError("a user must not be able to run two task timers simultaneously")
            except AppException as exc:
                assert exc.status_code == 409, exc

            # ── 9. pm (genuine project member, but NOT this task's
            # assignee — worker is) cannot stop worker's timer. Under
            # Assignee-Only Timer Control this is now rejected by the
            # assignee check itself (403), before it would even reach the
            # old "do you own an active entry here" check (400) — project
            # membership alone no longer grants timer control at all. ──────
            try:
                await stop_task_timer(task.id, tenant=pm_tenant)
                raise AssertionError("a non-assignee must never be able to stop someone else's timer")
            except AppException as exc:
                assert exc.status_code == 403, exc
                assert exc.code == "TASK_TIMER_ASSIGNEE_ONLY", exc
            # worker's own session must be completely unaffected by that attempt.
            still_active = await get_task_time_state(task.id, tenant=worker_tenant)
            assert still_active.is_active is True

            # ── 4. Stop succeeds; duration is positive/correct. ─────────────
            time.sleep(1.1)
            stop_result = await stop_task_timer(task.id, tenant=worker_tenant)
            assert stop_result.is_active is False
            assert stop_result.tracked_time_seconds >= 1, "at least ~1 second must have been recorded"
            first_total = stop_result.tracked_time_seconds

            # ── 8. Stopping again with no active timer is a clear error. ────
            try:
                await stop_task_timer(task.id, tenant=worker_tenant)
                raise AssertionError("stopping with no active timer must not succeed silently")
            except AppException as exc:
                assert exc.status_code == 400, exc

            # ── 5. A second session accumulates on top of the first. ────────
            await start_task_timer(task.id, tenant=worker_tenant)
            time.sleep(1.1)
            second_stop = await stop_task_timer(task.id, tenant=worker_tenant)
            assert second_stop.tracked_time_seconds >= first_total + 1, "the second session's time must add to, not replace, the first"

            # ── 13. Project Manager who IS the task's assignee can
            # start/stop — timer control comes from being the assignee,
            # not from project membership. ────────────────────────────────
            pm_start = await start_task_timer(pm_task.id, tenant=pm_tenant)
            assert pm_start.is_active is True
            pm_stop = await stop_task_timer(pm_task.id, tenant=pm_tenant)
            assert pm_stop.is_active is False

            # ── 13b. That SAME Project Manager, merely a project member
            # (not the assignee) of `task` — assigned to worker — has no
            # timer control over it at all, despite genuine, unchanged read/
            # manage access to the task itself (this is the exact
            # distinction Assignee-Only Timer Control draws: can_access_task
            # is no longer sufficient for Start/Stop). ───────────────────────
            try:
                await start_task_timer(task.id, tenant=pm_tenant)
                raise AssertionError("project membership alone must not grant timer control over a task assigned to someone else")
            except AppException as exc:
                assert exc.status_code == 403, exc
                assert exc.code == "TASK_TIMER_ASSIGNEE_ONLY", exc

            # ── 15. Admin/Owner have NO timer-control override. Owner is
            # rejected from `other_task` (assigned to worker, not them) —
            # the ability comes from being assignee, never from being
            # Admin/Owner (Phase 8 of the Assignee-Only Timer Control
            # spec). Owner only gains control by actually becoming the
            # assignee through the normal (separately-validated)
            # reassignment flow. ──────────────────────────────────────────
            try:
                await start_task_timer(other_task.id, tenant=owner_tenant)
                raise AssertionError("Owner must not be able to start a timer on a task assigned to someone else")
            except AppException as exc:
                assert exc.status_code == 403, exc
                assert exc.code == "TASK_TIMER_ASSIGNEE_ONLY", exc

            other_task.assignee_id = owner.id
            db.add(other_task)
            await db.commit()
            owner_start = await start_task_timer(other_task.id, tenant=owner_tenant)
            assert owner_start.is_active is True
            owner_stop = await stop_task_timer(other_task.id, tenant=owner_tenant)
            assert owner_stop.is_active is False

            # ── 10. Cross-tenant: outsider cannot even reach this org's task. ──
            try:
                await start_task_timer(task.id, tenant=outsider_tenant)
                raise AssertionError("a user from a different organization must not be able to start a timer on this task")
            except AppException as exc:
                assert exc.status_code == 404, exc

            # ── 16. Concurrency: the DB partial unique index rejects a second
            # active row for the same user even when the application-level
            # check is bypassed. Uses a second, independent DB session for
            # the colliding attempt — genuinely simulating two concurrent
            # requests (each gets its own session/connection in production)
            # rather than two calls sharing one session/transaction, which
            # would entangle the failed insert's rollback with the first
            # call's already-committed row. ──────────────────────────────
            repo = TaskTimeEntryRepository(db, org_id)
            entry1 = await repo.start(task_id, worker.id)
            entry1_id = entry1.id
            created_entry_ids.append(entry1_id)
            async with AsyncSessionLocal() as db2:
                repo2 = TaskTimeEntryRepository(db2, org_id)
                try:
                    await repo2.start(other_task_id, worker.id)
                    raise AssertionError("the database must reject a second concurrently-created active row for the same user")
                except DuplicateActiveTimerError:
                    pass

            entry1 = await repo.get_active_for_user_on_task(worker.id, task_id)
            assert entry1 is not None, "the first, legitimately-created entry must survive the second (rejected) attempt"
            await repo.stop(entry1)

            # Collect all entries created via the route calls above for cleanup.
            from sqlalchemy import select
            result = await db.execute(select(TaskTimeEntry).where(TaskTimeEntry.organization_id == org_id))
            created_entry_ids.extend([e.id for e in result.scalars().all() if e.id not in created_entry_ids])

        finally:
            if created_entry_ids:
                await db.execute(delete(TaskTimeEntry).where(TaskTimeEntry.id.in_(created_entry_ids)))
            await db.execute(delete(Task).where(Task.id.in_([task_id, other_task_id, outsider_task_id, pm_task_id])))
            await db.execute(delete(ProjectMembership).where(ProjectMembership.project_id == project_id))
            await db.execute(delete(Project).where(Project.id == project_id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_([org_id, other_org_id])))
            await db.execute(delete(Organization).where(Organization.id.in_([org_id, other_org_id])))
            await db.execute(delete(User).where(User.id.in_(user_ids)))
            await db.commit()

    await engine.dispose()


def test_task_time_tracking_foundation():
    asyncio.run(_scenario())
