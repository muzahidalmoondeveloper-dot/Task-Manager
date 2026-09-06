"""Regression tests for the Task List Working Time follow-up (bulk
Working Time aggregation for Task list/table/card surfaces —
app.repositories.task_time_entry_repository.TaskTimeEntryRepository.
get_task_time_summaries / POST /tasks/time-summaries).

Covers (see the spec's PART 37):
  1. a task with no entries -> 0 / 0
  2. one completed session -> exact total
  3. multiple completed sessions on the same task -> summed
  4. multiple tasks -> independent totals
  5. multiple users on the same task (completed) -> summed
  6. one active user -> active elapsed included
  7. multiple active users on the same task -> BOTH active elapsed
     amounts included (#7A: one active timer per user, not per task)
  8. completed + active on the same task -> no double counting
  9. >24h total -> exact seconds, no 24h wraparound
  10. another organization's entries are excluded
  11. an unrequested task_id is excluded from the response
  12. an empty task_ids list returns a safe, empty result
  13. authorization: a plain team_member with no access to a task never
      gets that task's summary; a Project Manager who IS a project member
      does; Admin/Owner gets everything; cross-tenant task_ids are excluded
  14. GET/Start/Stop single-task endpoints still work, and their
      `tracked_time_seconds`/`active_timer_count` now reflect the task's
      TRUE total (all users), not just the caller's own — the same fix
      that makes the bulk endpoint and the single-task endpoint agree.

Runs against the real database connection the app uses. Every row this
test creates is deleted before it returns.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete, select

from app.api.routes.tasks import get_task_time_state, get_task_time_summaries, start_task_timer, stop_task_timer
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import PROJECT_MANAGER, TEAM_MANAGER, TEAM_MEMBER
from app.core.tenant import TenantContext
from app.models.organization import Organization, OrganizationMembership
from app.models.project import Project, ProjectMembership
from app.models.task import Task
from app.models.task_time_entry import TaskTimeEntry
from app.models.user import User
from app.schemas.task_time_entry import TaskTimeSummariesRequest


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]

        owner = User(full_name="WT Owner", email=f"wt.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        worker = User(full_name="WT Worker", email=f"wt.worker.{suffix}@test.invalid", hashed_password="x", role=TEAM_MEMBER)
        other_member = User(full_name="WT Other Member", email=f"wt.other.{suffix}@test.invalid", hashed_password="x", role=TEAM_MEMBER)
        pm = User(full_name="WT PM", email=f"wt.pm.{suffix}@test.invalid", hashed_password="x", role=PROJECT_MANAGER)
        tm = User(full_name="WT TM", email=f"wt.tm.{suffix}@test.invalid", hashed_password="x", role=TEAM_MANAGER)
        outsider = User(full_name="WT Outsider", email=f"wt.outsider.{suffix}@test.invalid", hashed_password="x", role="owner")
        db.add_all([owner, worker, other_member, pm, tm, outsider])
        await db.commit()
        for u in (owner, worker, other_member, pm, tm, outsider):
            await db.refresh(u)

        org = Organization(name=f"WT Org {suffix}", slug=f"wt-org-{suffix}", owner_id=owner.id)
        other_org = Organization(name=f"WT Other Org {suffix}", slug=f"wt-other-org-{suffix}", owner_id=outsider.id)
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

        project = Project(name=f"WT Project {suffix}", created_by_id=owner.id, organization_id=org.id)
        db.add(project)
        await db.commit()
        await db.refresh(project)
        db.add(ProjectMembership(project_id=project.id, user_id=pm.id))
        await db.commit()

        # task_a: assigned to worker, under the PM's project (PM can see it).
        # task_b: independent task, also assigned to worker.
        # unrelated_task: assigned to other_member only — worker/pm/tm must
        #   never see its Working Time via the bulk endpoint.
        task_a = Task(name=f"WT Task A {suffix}", assignee_id=worker.id, project_id=project.id, created_by_id=owner.id, organization_id=org.id)
        task_b = Task(name=f"WT Task B {suffix}", assignee_id=worker.id, created_by_id=owner.id, organization_id=org.id)
        empty_task = Task(name=f"WT Empty Task {suffix}", assignee_id=worker.id, created_by_id=owner.id, organization_id=org.id)
        unrelated_task = Task(name=f"WT Unrelated Task {suffix}", assignee_id=other_member.id, created_by_id=owner.id, organization_id=org.id)
        outsider_task = Task(name=f"WT Outsider Task {suffix}", created_by_id=outsider.id, organization_id=other_org.id)
        db.add_all([task_a, task_b, empty_task, unrelated_task, outsider_task])
        await db.commit()
        for t in (task_a, task_b, empty_task, unrelated_task, outsider_task):
            await db.refresh(t)

        task_a_id, task_b_id, empty_task_id = task_a.id, task_b.id, empty_task.id
        unrelated_task_id, outsider_task_id = unrelated_task.id, outsider_task.id
        project_id, org_id, other_org_id = project.id, org.id, other_org.id
        user_ids = [owner.id, worker.id, other_member.id, pm.id, tm.id, outsider.id]

        worker_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[worker.id], user=worker, db=db)
        other_member_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[other_member.id], user=other_member, db=db)
        pm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[pm.id], user=pm, db=db)
        tm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[tm.id], user=tm, db=db)
        owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[owner.id], user=owner, db=db)

        created_entry_ids: list[int] = []

        def _mk_entry(task_id, user_id, *, started_delta, stopped_delta=None):
            now = datetime.now(timezone.utc)
            started_at = now + timedelta(seconds=started_delta)
            entry = TaskTimeEntry(
                task_id=task_id, user_id=user_id, organization_id=org_id,
                started_at=started_at,
                stopped_at=(now + timedelta(seconds=stopped_delta)) if stopped_delta is not None else None,
                duration_seconds=(stopped_delta - started_delta) if stopped_delta is not None else None,
            )
            db.add(entry)
            return entry

        try:
            # ── 12. Empty task_ids -> safe empty result. ─────────────────────
            empty_response = await get_task_time_summaries(TaskTimeSummariesRequest(task_ids=[]), tenant=worker_tenant)
            assert empty_response.items == {}

            # ── 1. A task with no entries at all -> excluded/zero, never an
            # error, never present with a bogus nonzero value. ───────────────
            zero_response = await get_task_time_summaries(TaskTimeSummariesRequest(task_ids=[empty_task_id]), tenant=worker_tenant)
            assert str(empty_task_id) in zero_response.items, "a visible task must appear even with zero recorded time"
            assert zero_response.items[str(empty_task_id)].working_time_seconds == 0
            assert zero_response.items[str(empty_task_id)].active_timer_count == 0

            # ── 2. One completed session -> exact total. ─────────────────────
            e1 = _mk_entry(task_a_id, worker.id, started_delta=-100, stopped_delta=-40)  # 60s
            await db.commit()
            created_entry_ids.append(e1.id)
            r2 = await get_task_time_summaries(TaskTimeSummariesRequest(task_ids=[task_a_id]), tenant=worker_tenant)
            assert r2.items[str(task_a_id)].working_time_seconds == 60

            # ── 3. Multiple completed sessions, same task -> summed. ─────────
            e2 = _mk_entry(task_a_id, worker.id, started_delta=-200, stopped_delta=-170)  # 30s
            await db.commit()
            created_entry_ids.append(e2.id)
            r3 = await get_task_time_summaries(TaskTimeSummariesRequest(task_ids=[task_a_id]), tenant=worker_tenant)
            assert r3.items[str(task_a_id)].working_time_seconds == 90

            # ── 4. Multiple tasks -> independent totals. ──────────────────────
            e3 = _mk_entry(task_b_id, worker.id, started_delta=-500, stopped_delta=-475)  # 25s
            await db.commit()
            created_entry_ids.append(e3.id)
            r4 = await get_task_time_summaries(TaskTimeSummariesRequest(task_ids=[task_a_id, task_b_id]), tenant=worker_tenant)
            assert r4.items[str(task_a_id)].working_time_seconds == 90
            assert r4.items[str(task_b_id)].working_time_seconds == 25

            # ── 5. Multiple users, same task (completed) -> summed. ──────────
            e4 = _mk_entry(task_a_id, pm.id, started_delta=-80, stopped_delta=-60)  # 20s, different user
            await db.commit()
            created_entry_ids.append(e4.id)
            r5 = await get_task_time_summaries(TaskTimeSummariesRequest(task_ids=[task_a_id]), tenant=worker_tenant)
            assert r5.items[str(task_a_id)].working_time_seconds == 110, "completed time across different users on the same task must be summed"

            # ── 9. >24h total -> exact seconds, no 24h wraparound. ────────────
            over_24h_seconds = 97805  # 27h 10m 5s
            e5 = _mk_entry(empty_task_id, worker.id, started_delta=-(over_24h_seconds + 5), stopped_delta=-5)
            await db.commit()
            created_entry_ids.append(e5.id)
            r9 = await get_task_time_summaries(TaskTimeSummariesRequest(task_ids=[empty_task_id]), tenant=worker_tenant)
            assert r9.items[str(empty_task_id)].working_time_seconds == over_24h_seconds

            # ── 6. One active user -> active elapsed included, no
            # double-counting on top of already-completed time (8). ──────────
            e6 = _mk_entry(task_b_id, worker.id, started_delta=-10)  # active, ~10s elapsed
            await db.commit()
            created_entry_ids.append(e6.id)
            r6 = await get_task_time_summaries(TaskTimeSummariesRequest(task_ids=[task_b_id]), tenant=worker_tenant)
            item = r6.items[str(task_b_id)]
            assert item.active_timer_count == 1
            assert item.working_time_seconds >= 25 + 9, "completed (25s) + active elapsed must both be present, not overwritten"
            assert item.working_time_seconds <= 25 + 15, "active elapsed must be a real ~10s figure, not fabricated"

            # ── 7. Multiple ACTIVE users on the SAME task -> both counted
            # (#7A: one active timer per user, not per task). ─────────────────
            e7 = _mk_entry(task_b_id, pm.id, started_delta=-10)  # a second, independent active user
            await db.commit()
            created_entry_ids.append(e7.id)
            r7 = await get_task_time_summaries(TaskTimeSummariesRequest(task_ids=[task_b_id]), tenant=worker_tenant)
            item7 = r7.items[str(task_b_id)]
            assert item7.active_timer_count == 2, "two independent users actively timing the same task must both be counted"
            # 25 completed + ~10 (worker) + ~10 (pm) ~= 45; must be roughly
            # double a single active user's contribution, not the same as r6.
            assert item7.working_time_seconds > item.working_time_seconds, "a second concurrent active timer must increase the task total"

            # cleanup task_b's active entries before continuing
            await db.execute(delete(TaskTimeEntry).where(TaskTimeEntry.id.in_([e6.id, e7.id])))
            created_entry_ids = [i for i in created_entry_ids if i not in (e6.id, e7.id)]
            await db.commit()

            # ── 10. Another organization's entries never leak in, even if
            # somehow the same task_id number existed there. ─────────────────
            outsider_entry = _mk_entry(outsider_task_id, outsider.id, started_delta=-40, stopped_delta=-10)
            outsider_entry.organization_id = other_org_id
            await db.commit()
            created_entry_ids.append(outsider_entry.id)
            r10 = await get_task_time_summaries(TaskTimeSummariesRequest(task_ids=[outsider_task_id]), tenant=worker_tenant)
            assert str(outsider_task_id) not in r10.items, "a cross-tenant task_id must never appear in the response"

            # ── 11. An unrequested task_id is simply excluded — requesting
            # [task_a] must never also return task_b's summary. ───────────────
            r11 = await get_task_time_summaries(TaskTimeSummariesRequest(task_ids=[task_a_id]), tenant=worker_tenant)
            assert list(r11.items.keys()) == [str(task_a_id)]

            # ── 13. Authorization mirrors can_access_task exactly. ────────────
            # other_member has no relation to task_a/task_b at all (not the
            # assignee, no project-manager access) -> both excluded.
            r13_other_member = await get_task_time_summaries(TaskTimeSummariesRequest(task_ids=[task_a_id, task_b_id]), tenant=other_member_tenant)
            assert r13_other_member.items == {}, "a user with no access to these tasks must see no Working Time for them"

            # tm: plain Team Manager, not assignee, not a project member ->
            # also excluded (matches can_access_task's existing behavior).
            r13_tm = await get_task_time_summaries(TaskTimeSummariesRequest(task_ids=[task_a_id]), tenant=tm_tenant)
            assert r13_tm.items == {}

            # pm: genuine ProjectMembership on task_a's project -> included;
            # task_b has no project at all -> excluded for pm.
            r13_pm = await get_task_time_summaries(TaskTimeSummariesRequest(task_ids=[task_a_id, task_b_id]), tenant=pm_tenant)
            assert str(task_a_id) in r13_pm.items
            assert str(task_b_id) not in r13_pm.items

            # owner: admin/owner sees everything in the org, including
            # unrelated_task (assigned to someone else entirely).
            r13_owner = await get_task_time_summaries(TaskTimeSummariesRequest(task_ids=[task_a_id, task_b_id, unrelated_task_id]), tenant=owner_tenant)
            assert set(r13_owner.items.keys()) == {str(task_a_id), str(task_b_id), str(unrelated_task_id)}

            # cross-org: an org-A tenant can never get an org-B task's summary
            # even if it somehow guesses the id.
            r13_cross = await get_task_time_summaries(TaskTimeSummariesRequest(task_ids=[outsider_task_id]), tenant=worker_tenant)
            assert r13_cross.items == {}

            # ── 14. Single-task endpoints still work, and now reflect the
            # task's TRUE total (all users) + active_timer_count. ────────────
            state = await get_task_time_state(task_a_id, tenant=worker_tenant)
            assert state.tracked_time_seconds == 110, "GET /tasks/{id}/time must agree with the bulk endpoint's task-total definition"
            assert state.active_timer_count == 0
            assert state.is_active is False

            start_result = await start_task_timer(task_a_id, tenant=worker_tenant)
            assert start_result.is_active is True
            assert start_result.active_timer_count == 1
            created_entry_ids_before_stop = (await db.execute(
                select(TaskTimeEntry.id).where(TaskTimeEntry.task_id == task_a_id, TaskTimeEntry.stopped_at.is_(None))
            )).scalars().all()
            created_entry_ids.extend(created_entry_ids_before_stop)

            stop_result = await stop_task_timer(task_a_id, tenant=worker_tenant)
            assert stop_result.is_active is False
            assert stop_result.active_timer_count == 0
            assert stop_result.tracked_time_seconds >= 110, "Start/Stop must add to, not reset, the pre-existing total"

        finally:
            if created_entry_ids:
                await db.execute(delete(TaskTimeEntry).where(TaskTimeEntry.id.in_(created_entry_ids)))
            await db.execute(delete(TaskTimeEntry).where(TaskTimeEntry.task_id.in_([task_a_id, task_b_id, empty_task_id, unrelated_task_id, outsider_task_id])))
            await db.execute(delete(Task).where(Task.id.in_([task_a_id, task_b_id, empty_task_id, unrelated_task_id, outsider_task_id])))
            await db.execute(delete(ProjectMembership).where(ProjectMembership.project_id == project_id))
            await db.execute(delete(Project).where(Project.id == project_id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_([org_id, other_org_id])))
            await db.execute(delete(Organization).where(Organization.id.in_([org_id, other_org_id])))
            await db.execute(delete(User).where(User.id.in_(user_ids)))
            await db.commit()

    await engine.dispose()


def test_task_list_working_time():
    asyncio.run(_scenario())
