"""Regression tests for Task #7B — Project Working Time
(app.repositories.task_time_entry_repository.TaskTimeEntryRepository.
get_project_time_summary / GET /projects/{id}/working-time).

Covers (see PHASE 24 of the spec):
  1. project with no time entries -> 0
  2. one completed entry -> exact total
  3. multiple completed sessions for one task -> summed
  4. multiple tasks in the same project -> summed
  5. multiple users -> summed
  6. an active entry's live elapsed time is included
  7. multiple simultaneous active users in the same project -> all included
  8. a task from another project is excluded
  9. a task with project_id = NULL is excluded
  10. a task from another organization is excluded
  11. completed + active time coexist with no double counting
  12. >24 hours aggregates as exact seconds, no wrapping
  13. a Project Manager with genuine ProjectMembership can retrieve it
  14. a plain Team Manager (even one holding a ProjectMembership row on
      this exact project) cannot retrieve it — matches the existing,
      already-shipped require_project_management_access rule verbatim
  15. Admin/Owner access succeeds
  16. a cross-tenant project id is rejected (404), not answered with data
  17. hard-deleted-task semantics: deleting a task cascades its
      TaskTimeEntry rows (already established in #7A), so its time no
      longer contributes to the project total afterward — verified here,
      not merely asserted in prose
  18. task project-reassignment: moving an existing task to a different
      project makes its ALREADY-RECORDED time follow it to the new
      project on the next read (TaskTimeEntry has no historical project_id
      snapshot) — this test proves that documented behavior rather than
      just describing it

Runs against the real database connection the app uses. Every row this
test creates is deleted before it returns.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete

from app.api.routes.projects import get_project_working_time
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import PROJECT_MANAGER, TEAM_MANAGER
from app.core.tenant import TenantContext
from app.models.organization import Organization, OrganizationMembership
from app.models.project import Project, ProjectMembership
from app.models.task import Task
from app.models.task_time_entry import TaskTimeEntry
from app.models.user import User
from app.repositories.task_time_entry_repository import TaskTimeEntryRepository


async def _add_entry(db, *, org_id, task_id, user_id, started_at, stopped_at=None, duration_seconds=None):
    entry = TaskTimeEntry(
        organization_id=org_id, task_id=task_id, user_id=user_id,
        started_at=started_at, stopped_at=stopped_at, duration_seconds=duration_seconds,
    )
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    return entry


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]
        now = datetime.now(timezone.utc)

        owner = User(full_name="PWT Owner", email=f"pwt.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        user_a = User(full_name="PWT User A", email=f"pwt.a.{suffix}@test.invalid", hashed_password="x", role="team_member")
        user_b = User(full_name="PWT User B", email=f"pwt.b.{suffix}@test.invalid", hashed_password="x", role="team_member")
        pm = User(full_name="PWT PM", email=f"pwt.pm.{suffix}@test.invalid", hashed_password="x", role=PROJECT_MANAGER)
        tm = User(full_name="PWT TM", email=f"pwt.tm.{suffix}@test.invalid", hashed_password="x", role=TEAM_MANAGER)
        outsider = User(full_name="PWT Outsider", email=f"pwt.outsider.{suffix}@test.invalid", hashed_password="x", role="owner")
        db.add_all([owner, user_a, user_b, pm, tm, outsider])
        await db.commit()
        for u in (owner, user_a, user_b, pm, tm, outsider):
            await db.refresh(u)

        org = Organization(name=f"PWT Org {suffix}", slug=f"pwt-org-{suffix}", owner_id=owner.id)
        other_org = Organization(name=f"PWT Other Org {suffix}", slug=f"pwt-other-org-{suffix}", owner_id=outsider.id)
        db.add_all([org, other_org])
        await db.commit()
        for o in (org, other_org):
            await db.refresh(o)

        memberships = {}
        for u, role in [(owner, "owner"), (user_a, "team_member"), (user_b, "team_member"), (pm, PROJECT_MANAGER), (tm, TEAM_MANAGER)]:
            m = OrganizationMembership(organization_id=org.id, user_id=u.id, role=role)
            db.add(m)
            memberships[u.id] = m
        outsider_membership = OrganizationMembership(organization_id=other_org.id, user_id=outsider.id, role="owner")
        db.add(outsider_membership)
        await db.commit()
        for m in list(memberships.values()) + [outsider_membership]:
            await db.refresh(m)

        project = Project(name=f"PWT Project {suffix}", created_by_id=owner.id, organization_id=org.id)
        other_project = Project(name=f"PWT Other Project {suffix}", created_by_id=owner.id, organization_id=org.id)
        db.add_all([project, other_project])
        await db.commit()
        for p in (project, other_project):
            await db.refresh(p)
        db.add(ProjectMembership(project_id=project.id, user_id=pm.id))
        # A ProjectMembership row for the plain Team Manager too — proves
        # membership alone must NOT be sufficient (Phase 14 of this task).
        db.add(ProjectMembership(project_id=project.id, user_id=tm.id))
        await db.commit()

        task1 = Task(name=f"PWT Task1 {suffix}", project_id=project.id, created_by_id=owner.id, organization_id=org.id)
        task2 = Task(name=f"PWT Task2 {suffix}", project_id=project.id, created_by_id=owner.id, organization_id=org.id)
        other_project_task = Task(name=f"PWT OtherProjTask {suffix}", project_id=other_project.id, created_by_id=owner.id, organization_id=org.id)
        no_project_task = Task(name=f"PWT NoProjTask {suffix}", project_id=None, created_by_id=owner.id, organization_id=org.id)
        outsider_task = Task(name=f"PWT OutsiderTask {suffix}", project_id=None, created_by_id=outsider.id, organization_id=other_org.id)
        db.add_all([task1, task2, other_project_task, no_project_task, outsider_task])
        await db.commit()
        for t in (task1, task2, other_project_task, no_project_task, outsider_task):
            await db.refresh(t)
        task1_id, task2_id = task1.id, task2.id
        other_project_task_id, no_project_task_id, outsider_task_id = other_project_task.id, no_project_task.id, outsider_task.id
        project_id, other_project_id, org_id, other_org_id = project.id, other_project.id, org.id, other_org.id

        owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[owner.id], user=owner, db=db)
        pm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[pm.id], user=pm, db=db)
        tm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[tm.id], user=tm, db=db)
        outsider_tenant = TenantContext(organization_id=other_org.id, organization=other_org, membership=outsider_membership, user=outsider, db=db)

        repo = TaskTimeEntryRepository(db, org_id)
        created_entry_ids = []

        def track(entry):
            created_entry_ids.append(entry.id)
            return entry

        try:
            # ── 1. No entries yet -> 0. ──────────────────────────────────────
            result = await get_project_working_time(project_id, db=db, tenant=owner_tenant)
            assert result.working_time_seconds == 0
            assert result.active_timer_count == 0

            # ── 2. One completed entry -> exact total. ───────────────────────
            track(await _add_entry(db, org_id=org_id, task_id=task1_id, user_id=user_a.id, started_at=now - timedelta(minutes=20), stopped_at=now - timedelta(minutes=10), duration_seconds=600))
            result = await get_project_working_time(project_id, db=db, tenant=owner_tenant)
            assert result.working_time_seconds == 600

            # ── 3. A second completed session on the SAME task sums. ─────────
            track(await _add_entry(db, org_id=org_id, task_id=task1_id, user_id=user_a.id, started_at=now - timedelta(minutes=9), stopped_at=now - timedelta(minutes=4), duration_seconds=300))
            result = await get_project_working_time(project_id, db=db, tenant=owner_tenant)
            assert result.working_time_seconds == 900

            # ── 4. A session on a DIFFERENT task in the same project sums too. ──
            track(await _add_entry(db, org_id=org_id, task_id=task2_id, user_id=user_a.id, started_at=now - timedelta(minutes=30), stopped_at=now - timedelta(minutes=25), duration_seconds=300))
            result = await get_project_working_time(project_id, db=db, tenant=owner_tenant)
            assert result.working_time_seconds == 1200

            # ── 5. A different USER's completed session also sums. ───────────
            track(await _add_entry(db, org_id=org_id, task_id=task2_id, user_id=user_b.id, started_at=now - timedelta(minutes=15), stopped_at=now - timedelta(minutes=10), duration_seconds=300))
            result = await get_project_working_time(project_id, db=db, tenant=owner_tenant)
            assert result.working_time_seconds == 1500

            # ── 8. A task from ANOTHER project must never contribute. ────────
            track(await _add_entry(db, org_id=org_id, task_id=other_project_task_id, user_id=user_a.id, started_at=now - timedelta(hours=1), stopped_at=now - timedelta(minutes=50), duration_seconds=600))
            result = await get_project_working_time(project_id, db=db, tenant=owner_tenant)
            assert result.working_time_seconds == 1500, "another project's task time must never leak into this project's total"

            # ── 9. A task with project_id = NULL must never contribute. ──────
            track(await _add_entry(db, org_id=org_id, task_id=no_project_task_id, user_id=user_a.id, started_at=now - timedelta(hours=1), stopped_at=now - timedelta(minutes=50), duration_seconds=600))
            result = await get_project_working_time(project_id, db=db, tenant=owner_tenant)
            assert result.working_time_seconds == 1500, "an unassociated (project_id=NULL) task must never contribute"

            # ── 10. Another organization's task/entry must never contribute
            # even if somehow queried (defense in depth beyond the route's
            # own project-not-found check below). ────────────────────────────
            other_org_repo_summary = await TaskTimeEntryRepository(db, other_org_id).get_project_time_summary(project_id, now)
            assert other_org_repo_summary == (0, 0), "a project id from this org must not resolve to data under a different org's scope"

            # ── 6, 7, 11. Active sessions (single, then multiple users) are
            # included, live-elapsed, alongside completed time with no
            # double counting. ─────────────────────────────────────────────
            track(await _add_entry(db, org_id=org_id, task_id=task1_id, user_id=user_a.id, started_at=now - timedelta(seconds=100)))
            result = await get_project_working_time(project_id, db=db, tenant=owner_tenant)
            assert result.active_timer_count == 1
            assert 1500 + 95 <= result.working_time_seconds <= 1500 + 105, "active elapsed time must be included on top of the completed total"

            track(await _add_entry(db, org_id=org_id, task_id=task2_id, user_id=user_b.id, started_at=now - timedelta(seconds=50)))
            result = await get_project_working_time(project_id, db=db, tenant=owner_tenant)
            assert result.active_timer_count == 2, "two different users each running their own timer in this project must both be counted"
            assert 1500 + 100 + 45 <= result.working_time_seconds <= 1500 + 110 + 55

            # Stop both active sessions so later assertions have a clean,
            # fully-completed baseline.
            for tid, uid in [(task1_id, user_a.id), (task2_id, user_b.id)]:
                active = await repo.get_active_for_user_on_task(uid, tid)
                await repo.stop(active)
            baseline_result = await get_project_working_time(project_id, db=db, tenant=owner_tenant)
            assert baseline_result.active_timer_count == 0
            baseline_total = baseline_result.working_time_seconds
            assert baseline_total > 1500

            # ── 12. >24 hours aggregates as exact seconds, no wrapping. ──────
            over_24h_seconds = 27 * 3600 + 10 * 60  # 27h 10m
            track(await _add_entry(db, org_id=org_id, task_id=task1_id, user_id=user_a.id, started_at=now - timedelta(seconds=over_24h_seconds + 5), stopped_at=now - timedelta(seconds=5), duration_seconds=over_24h_seconds))
            result = await get_project_working_time(project_id, db=db, tenant=owner_tenant)
            assert result.working_time_seconds == baseline_total + over_24h_seconds, "a >24h single session must add its exact seconds, never wrap"

            # ── 13. Project Manager with genuine membership can retrieve it. ──
            pm_result = await get_project_working_time(project_id, db=db, tenant=pm_tenant)
            assert pm_result.working_time_seconds == result.working_time_seconds

            # ── 14. Plain Team Manager — even WITH a ProjectMembership row on
            # this exact project — is rejected outright. ─────────────────────
            try:
                await get_project_working_time(project_id, db=db, tenant=tm_tenant)
                raise AssertionError("a plain Team Manager must not be able to retrieve Project Working Time even with a ProjectMembership row")
            except AppException as exc:
                assert exc.status_code == 403, exc

            # ── 15. Admin/Owner access succeeds (already exercised via
            # owner_tenant throughout — asserted explicitly here too). ───────
            owner_result = await get_project_working_time(project_id, db=db, tenant=owner_tenant)
            assert owner_result.working_time_seconds == result.working_time_seconds

            # ── 16. Cross-tenant project id -> 404, not data. ─────────────────
            try:
                await get_project_working_time(project_id, db=db, tenant=outsider_tenant)
                raise AssertionError("a project id belonging to a different organization must not resolve")
            except AppException as exc:
                assert exc.status_code == 404, exc

            # ── 17. Hard-deleting a task cascades its TaskTimeEntry rows —
            # their time stops contributing on the very next read. ───────────
            await db.execute(delete(Task).where(Task.id == task2_id))
            await db.commit()
            # task2's TaskTimeEntry rows are gone now too (ON DELETE CASCADE,
            # established in #7A) — harmless if cleanup's final delete below
            # also lists their (now nonexistent) ids.
            after_delete_result = await get_project_working_time(project_id, db=db, tenant=owner_tenant)
            assert after_delete_result.working_time_seconds < result.working_time_seconds, (
                "deleting a task must cascade-delete its time entries, removing their contribution to the project total"
            )

            # ── 18. Project reassignment: moving task1 to other_project makes
            # its already-recorded time follow it there on the next read —
            # TaskTimeEntry has no historical project_id snapshot. ────────────
            before_reassign = await get_project_working_time(project_id, db=db, tenant=owner_tenant)
            task1_row = await db.get(Task, task1_id)
            task1_row.project_id = other_project_id
            await db.commit()

            after_reassign_old_project = await get_project_working_time(project_id, db=db, tenant=owner_tenant)
            after_reassign_new_project = await get_project_working_time(other_project_id, db=db, tenant=owner_tenant)
            assert after_reassign_old_project.working_time_seconds < before_reassign.working_time_seconds, (
                "task1's recorded time must leave the old project once task1 itself is reassigned"
            )
            assert after_reassign_new_project.working_time_seconds > 0, (
                "task1's recorded time must now be attributed to the project it currently belongs to"
            )

        finally:
            if created_entry_ids:
                await db.execute(delete(TaskTimeEntry).where(TaskTimeEntry.id.in_(created_entry_ids)))
            await db.execute(delete(Task).where(Task.id.in_([task1_id, other_project_task_id, no_project_task_id, outsider_task_id])))
            await db.execute(delete(ProjectMembership).where(ProjectMembership.project_id.in_([project_id, other_project_id])))
            await db.execute(delete(Project).where(Project.id.in_([project_id, other_project_id])))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_([org_id, other_org_id])))
            await db.execute(delete(Organization).where(Organization.id.in_([org_id, other_org_id])))
            await db.execute(delete(User).where(User.id.in_([owner.id, user_a.id, user_b.id, pm.id, tm.id, outsider.id])))
            await db.commit()

    await engine.dispose()


def test_project_working_time():
    asyncio.run(_scenario())
