"""Regression tests for the Start/Stop-from-list follow-up — current-user
timer state on the bulk Working Time endpoint
(app.schemas.task_time_entry.TaskTimeSummaryItem.current_user_is_active /
TaskTimeSummariesResponse.current_user_has_active_timer /
current_user_active_task_id, POST /tasks/time-summaries).

Covers (see the spec's PHASE 20):
  1. no active timer anywhere -> current_user_is_active False on every item,
     current_user_has_active_timer False, current_user_active_task_id None
  2. current user active on Task A -> Task A's current_user_is_active True
  3. another user active on Task A, caller not active -> active_timer_count
     > 0 but current_user_is_active False (the exact bug this follow-up
     exists to prevent: active_timer_count must never be mistaken for "it's
     my timer")
  4. Assignee-Only Timer Control follow-up: a non-assignee (even
     Owner/Admin) can no longer Start a timer on a task assigned to
     someone else at all — verified directly against the route (403
     TASK_TIMER_ASSIGNEE_ONLY). Since a Task has exactly one assignee_id,
     this makes "two different users both actively timing the same task"
     unreachable through the API going forward (Phase 16 of that spec).
     The bulk aggregation math for that state (active_timer_count == 2,
     each caller's own current_user_is_active independent of the other)
     is still exercised here, but via direct repository insertion — the
     same technique used to model pre-existing/legacy data — never via a
     route call that the new rule would reject.
  5. current user has an active timer on a task OUTSIDE the requested
     task_ids -> current_user_has_active_timer True,
     current_user_active_task_id None (not leaked into an unrelated batch),
     and every requested item's current_user_is_active is False
  6. Start from the (already-existing) route succeeds and is immediately
     reflected in the next bulk summary
  7. Stop from the (already-existing) route succeeds and is immediately
     reflected in the next bulk summary
  8. a different user's Stop attempt on the first user's session fails
     (ownership unchanged by this follow-up)
  9. the global one-active-timer-per-user rule is still enforced (starting
     on a second task while one is active is still rejected)
  10. cross-tenant isolation: current_user_is_active is never computed from
      or leaked across another organization's rows
  11. permissions are unchanged (a plain team_member with no access to a
      task still gets nothing for it, exactly as in the base bulk endpoint
      tests)
  12. the Working Time aggregate itself is unaffected by these additive
      fields (still correct)
  13. Activity Log still records task.timer_started / task.timer_stopped
  14. no N+1 query growth from the added current-user lookup (measured)

Runs against the real database connection the app uses. Every row this
test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete, event, select

from app.api.routes.tasks import get_task_time_summaries, start_task_timer, stop_task_timer
from app.core.activity_actions import TASK_TIMER_STARTED, TASK_TIMER_STOPPED
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import TEAM_MEMBER
from app.core.tenant import TenantContext
from app.models.activity_log import ActivityLog
from app.models.organization import Organization, OrganizationMembership
from app.models.task import Task
from app.models.task_time_entry import TaskTimeEntry
from app.models.user import User
from app.repositories.task_time_entry_repository import TaskTimeEntryRepository
from app.schemas.task_time_entry import TaskTimeSummariesRequest


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]

        owner = User(full_name="SS Owner", email=f"ss.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        user_a = User(full_name="SS User A", email=f"ss.a.{suffix}@test.invalid", hashed_password="x", role=TEAM_MEMBER)
        user_b = User(full_name="SS User B", email=f"ss.b.{suffix}@test.invalid", hashed_password="x", role=TEAM_MEMBER)
        other_member = User(full_name="SS Other Member", email=f"ss.other.{suffix}@test.invalid", hashed_password="x", role=TEAM_MEMBER)
        outsider = User(full_name="SS Outsider", email=f"ss.outsider.{suffix}@test.invalid", hashed_password="x", role="owner")
        db.add_all([owner, user_a, user_b, other_member, outsider])
        await db.commit()
        for u in (owner, user_a, user_b, other_member, outsider):
            await db.refresh(u)

        org = Organization(name=f"SS Org {suffix}", slug=f"ss-org-{suffix}", owner_id=owner.id)
        other_org = Organization(name=f"SS Other Org {suffix}", slug=f"ss-other-org-{suffix}", owner_id=outsider.id)
        db.add_all([org, other_org])
        await db.commit()
        for o in (org, other_org):
            await db.refresh(o)

        memberships = {}
        for u, role in [(owner, "owner"), (user_a, TEAM_MEMBER), (user_b, TEAM_MEMBER), (other_member, TEAM_MEMBER)]:
            m = OrganizationMembership(organization_id=org.id, user_id=u.id, role=role)
            db.add(m)
            memberships[u.id] = m
        outsider_membership = OrganizationMembership(organization_id=other_org.id, user_id=outsider.id, role="owner")
        db.add(outsider_membership)
        await db.commit()

        # task_x: assignee is user_a (so both user_a and user_b need
        # access — give user_b access by also assigning tasks to them, but
        # to test "two different users on the same task" we need both to
        # be able to reach it; simplest is to make it unassigned, which is
        # only reachable by admins under can_access_task's existing rule
        # ("the task's assignee" / admin-owner / PM+membership) — so we use
        # the owner tenant to start/stop as "user_a"/"user_b" isn't
        # possible for a non-owner without being the assignee. Instead we
        # give task_x no assignee and drive its timers as the owner acting
        # AS each user is not supported by this app's auth model (a
        # TenantContext IS the acting user) — so we model "two different
        # users on the same task" the way #7A's own test suite does: an
        # owner (admin/owner: any task) and the assignee (their own task)
        # both legitimately reaching the same task.
        task_x = Task(name=f"SS Task X {suffix}", assignee_id=user_a.id, created_by_id=owner.id, organization_id=org.id)
        task_y = Task(name=f"SS Task Y {suffix}", assignee_id=user_a.id, created_by_id=owner.id, organization_id=org.id)
        unrelated_task = Task(name=f"SS Unrelated {suffix}", assignee_id=other_member.id, created_by_id=owner.id, organization_id=org.id)
        outsider_task = Task(name=f"SS Outsider Task {suffix}", created_by_id=outsider.id, organization_id=other_org.id)
        db.add_all([task_x, task_y, unrelated_task, outsider_task])
        await db.commit()
        for t in (task_x, task_y, unrelated_task, outsider_task):
            await db.refresh(t)

        task_x_id, task_y_id, unrelated_task_id, outsider_task_id = task_x.id, task_y.id, unrelated_task.id, outsider_task.id
        org_id, other_org_id = org.id, other_org.id
        user_ids = [owner.id, user_a.id, user_b.id, other_member.id, outsider.id]

        user_a_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[user_a.id], user=user_a, db=db)
        other_member_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[other_member.id], user=other_member, db=db)
        owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[owner.id], user=owner, db=db)

        created_entry_ids: list[int] = []

        try:
            # ── 1. No active timer anywhere -> everything False/None. ────────
            r1 = await get_task_time_summaries(TaskTimeSummariesRequest(task_ids=[task_x_id, task_y_id]), tenant=user_a_tenant)
            assert r1.current_user_has_active_timer is False
            assert r1.current_user_active_task_id is None
            assert r1.items[str(task_x_id)].current_user_is_active is False
            assert r1.items[str(task_y_id)].current_user_is_active is False

            # ── 6. Start from the existing route succeeds. ────────────────────
            start_result = await start_task_timer(task_x_id, tenant=user_a_tenant)
            assert start_result.is_active is True
            created_entry_ids.append(
                (await db.execute(select(TaskTimeEntry.id).where(TaskTimeEntry.task_id == task_x_id, TaskTimeEntry.user_id == user_a.id, TaskTimeEntry.stopped_at.is_(None)))).scalar_one()
            )

            # ── 2. Immediately reflected in the bulk summary. ─────────────────
            r2 = await get_task_time_summaries(TaskTimeSummariesRequest(task_ids=[task_x_id, task_y_id]), tenant=user_a_tenant)
            assert r2.current_user_has_active_timer is True
            assert r2.current_user_active_task_id == task_x_id
            assert r2.items[str(task_x_id)].current_user_is_active is True
            assert r2.items[str(task_x_id)].active_timer_count == 1
            # ── (Phase 3/5) Task Y must show Start-disabled state: caller has
            # an active timer, but not on Task Y. ─────────────────────────────
            assert r2.items[str(task_y_id)].current_user_is_active is False

            # ── 3. Another user (user_b has no access to task_x — it's
            # assigned to user_a only) — use owner instead, who legitimately
            # sees task_x as admin/owner, to prove active_timer_count > 0
            # does NOT imply current_user_is_active for a DIFFERENT caller. ──
            r3 = await get_task_time_summaries(TaskTimeSummariesRequest(task_ids=[task_x_id]), tenant=owner_tenant)
            assert r3.items[str(task_x_id)].active_timer_count == 1, "the aggregate must show a timer is running"
            assert r3.items[str(task_x_id)].current_user_is_active is False, "active_timer_count > 0 must NEVER imply current_user_is_active for a caller who isn't running it"
            assert r3.current_user_has_active_timer is False, "owner has no timer of their own running"

            # ── 4. Assignee-Only Timer Control: owner is NOT task_x's
            # assignee (user_a is) — admin/owner privilege no longer grants
            # timer control at all, so the route rejects this outright. ──────
            try:
                await start_task_timer(task_x_id, tenant=owner_tenant)
                raise AssertionError("Owner must not be able to start a timer on a task assigned to someone else")
            except AppException as exc:
                assert exc.status_code == 403, exc
                assert exc.code == "TASK_TIMER_ASSIGNEE_ONLY", exc

            # The DB model/constraint for a second concurrently-active entry
            # on the same task is unchanged (Phase 16/17) — only the new
            # rule's application-level Start authorization prevents it from
            # being reachable through the route. Insert owner's second
            # active entry directly via the repository (bypassing route
            # authorization on purpose) purely to prove the bulk
            # aggregation still handles that legacy-shaped state correctly.
            owner_entry = await TaskTimeEntryRepository(db, org_id).start(task_x_id, owner.id)
            created_entry_ids.append(owner_entry.id)

            r4_owner = await get_task_time_summaries(TaskTimeSummariesRequest(task_ids=[task_x_id]), tenant=owner_tenant)
            assert r4_owner.items[str(task_x_id)].active_timer_count == 2, "two independent users actively timing the same task must both be counted"
            assert r4_owner.items[str(task_x_id)].current_user_is_active is True, "owner's own summary must show their own timer as active"

            r4_user_a = await get_task_time_summaries(TaskTimeSummariesRequest(task_ids=[task_x_id]), tenant=user_a_tenant)
            assert r4_user_a.items[str(task_x_id)].active_timer_count == 2
            assert r4_user_a.items[str(task_x_id)].current_user_is_active is True, "user_a's own summary must still show THEIR OWN timer as active, independent of owner's"

            # ── 8. A different user cannot stop another user's timer —
            # ownership semantics are unchanged by this follow-up. ────────────
            try:
                await stop_task_timer(task_x_id, tenant=other_member_tenant)
                raise AssertionError("other_member has no active timer on task_x and no access to it at all — must fail")
            except Exception as exc:
                assert getattr(exc, "status_code", None) in (400, 403), exc
            # user_a's timer must be completely unaffected.
            still_active = await get_task_time_summaries(TaskTimeSummariesRequest(task_ids=[task_x_id]), tenant=user_a_tenant)
            assert still_active.items[str(task_x_id)].current_user_is_active is True

            # ── 9. The global one-active-timer-per-user rule still applies:
            # user_a already active on task_x cannot also start task_y. ──────
            try:
                await start_task_timer(task_y_id, tenant=user_a_tenant)
                raise AssertionError("a user must not be able to run two task timers simultaneously")
            except AppException as exc:
                assert exc.status_code == 409, exc

            # ── 5. user_a's active timer (on task_x) must not leak into a
            # bulk request that only asks about task_y — has_active_timer
            # stays True, but the task id is withheld since it isn't part of
            # this particular batch. ──────────────────────────────────────────
            r5 = await get_task_time_summaries(TaskTimeSummariesRequest(task_ids=[task_y_id]), tenant=user_a_tenant)
            assert r5.current_user_has_active_timer is True
            assert r5.current_user_active_task_id is None, "an active task_id outside the requested batch must not be exposed"
            assert r5.items[str(task_y_id)].current_user_is_active is False

            # ── 7. Stop succeeds and is immediately reflected. ────────────────
            stop_result = await stop_task_timer(task_x_id, tenant=user_a_tenant)
            assert stop_result.is_active is False
            r7 = await get_task_time_summaries(TaskTimeSummariesRequest(task_ids=[task_x_id]), tenant=user_a_tenant)
            assert r7.items[str(task_x_id)].current_user_is_active is False
            assert r7.current_user_has_active_timer is False
            # owner's independent timer on the same task must be unaffected.
            r7_owner = await get_task_time_summaries(TaskTimeSummariesRequest(task_ids=[task_x_id]), tenant=owner_tenant)
            assert r7_owner.items[str(task_x_id)].current_user_is_active is True
            assert r7_owner.items[str(task_x_id)].active_timer_count == 1

            # owner is not task_x's assignee, so the route itself would now
            # reject even Stopping their own (legacy-simulated) entry —
            # clean it up directly via the repository instead, consistent
            # with how it was created above.
            owner_active_entry = await TaskTimeEntryRepository(db, org_id).get_active_for_user_on_task(owner.id, task_x_id)
            assert owner_active_entry is not None
            await TaskTimeEntryRepository(db, org_id).stop(owner_active_entry)

            # ── 12. The Working Time aggregate itself is unaffected by these
            # additive fields — still correct (sanity check). ─────────────────
            r12 = await get_task_time_summaries(TaskTimeSummariesRequest(task_ids=[task_x_id]), tenant=user_a_tenant)
            assert r12.items[str(task_x_id)].working_time_seconds >= 0
            assert r12.items[str(task_x_id)].active_timer_count == 0

            # ── 13. Activity Log still records timer_started/timer_stopped
            # for successful, route-driven Start/Stop calls (user_a's) —
            # owner's entry above was deliberately created/stopped directly
            # via the repository (bypassing the route, since the route now
            # correctly rejects it), so it never went through
            # activity_service.record() at all; that absence is itself
            # correct — only actual route-level success ever logs. ──────────
            log_actions = (await db.execute(
                select(ActivityLog.action).where(
                    ActivityLog.organization_id == org_id,
                    ActivityLog.entity_id == task_x_id,
                    ActivityLog.action.in_([TASK_TIMER_STARTED, TASK_TIMER_STOPPED]),
                )
            )).scalars().all()
            assert log_actions.count(TASK_TIMER_STARTED) >= 1, "user_a's successful Start must be logged"
            assert log_actions.count(TASK_TIMER_STOPPED) >= 1, "user_a's successful Stop must be logged"

            # ── 11. Permissions unchanged: other_member has no access to
            # task_x/task_y at all -> empty items, exactly like the base
            # bulk endpoint's own authorization tests. ─────────────────────────
            r11 = await get_task_time_summaries(TaskTimeSummariesRequest(task_ids=[task_x_id, task_y_id]), tenant=other_member_tenant)
            assert r11.items == {}
            assert r11.current_user_has_active_timer is False

            # ── 10. Cross-tenant isolation: an org-A tenant never gets
            # current_user_is_active computed against another org's rows,
            # and an org-B task_id is excluded entirely. ───────────────────────
            r10 = await get_task_time_summaries(TaskTimeSummariesRequest(task_ids=[outsider_task_id]), tenant=user_a_tenant)
            assert r10.items == {}
            assert r10.current_user_has_active_timer is False

            # ── 14. No N+1 growth from the added current-user lookup. ────────
            query_count = {"n": 0}

            def _count(conn, cursor, statement, parameters, context, executemany):
                query_count["n"] += 1

            sync_engine = engine.sync_engine
            event.listen(sync_engine, "before_cursor_execute", _count)
            try:
                small_ids = [task_x_id, task_y_id]
                query_count["n"] = 0
                await get_task_time_summaries(TaskTimeSummariesRequest(task_ids=small_ids), tenant=owner_tenant)
                small_count = query_count["n"]

                # Simulate a much larger visible list by repeating the same
                # two real ids many times (de-duped server-side) plus
                # padding with the same ids again — what matters is that
                # the query COUNT (not row count) stays flat; duplicate ids
                # collapse via the route's own de-dupe, so pad with the
                # unrelated/outsider ids too (still constant query cost).
                large_ids = [task_x_id, task_y_id, unrelated_task_id, outsider_task_id] * 50
                query_count["n"] = 0
                await get_task_time_summaries(TaskTimeSummariesRequest(task_ids=large_ids[:500]), tenant=owner_tenant)
                large_count = query_count["n"]
            finally:
                event.remove(sync_engine, "before_cursor_execute", _count)

            assert small_count == large_count, f"query count must stay constant regardless of list size (got {small_count} vs {large_count})"

        finally:
            if created_entry_ids:
                await db.execute(delete(TaskTimeEntry).where(TaskTimeEntry.id.in_(created_entry_ids)))
            await db.execute(delete(TaskTimeEntry).where(TaskTimeEntry.task_id.in_([task_x_id, task_y_id, unrelated_task_id, outsider_task_id])))
            await db.execute(delete(ActivityLog).where(ActivityLog.organization_id.in_([org_id, other_org_id])))
            await db.execute(delete(Task).where(Task.id.in_([task_x_id, task_y_id, unrelated_task_id, outsider_task_id])))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_([org_id, other_org_id])))
            await db.execute(delete(Organization).where(Organization.id.in_([org_id, other_org_id])))
            await db.execute(delete(User).where(User.id.in_(user_ids)))
            await db.commit()

    await engine.dispose()


def test_task_list_start_stop():
    asyncio.run(_scenario())
