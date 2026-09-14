"""Regression tests for the Team Manager Task-update authorization bug fix
(app.api.routes.tasks.require_task_update_access, update_task,
update_task_status).

EXACT REPORTED BUG (the screenshot scenario): a Team Manager's own Task —
assigned directly to them, belonging to a Team they manage, visible in
"My Tasks" — could not have its status changed. `PATCH /tasks/{id}/status`
had its own, SECOND, stale authorization check (`if not
tenant.is_admin_or_owner: raise ...`) completely independent of the
canonical `require_task_update_access()` gate `PATCH /tasks/{id}` used —
any caller who wasn't literally TEAM_MEMBER or Owner/Admin (i.e. exactly a
Team Manager) was flatly rejected with "You are not allowed to update
tasks.", regardless of being the task's own assignee or the manager of
its Team.

A SECOND, independently-discovered bug in the same investigation:
`require_task_update_access()` itself granted `is_manager_or_above`
(true for Team Manager, not just Owner/Admin) an UNCONDITIONAL,
organization-wide pass — the exact "if is_team_manager: allow"
anti-pattern the product rules forbid. A Team Manager could update ANY
Task in the org via `PATCH /tasks/{id}`, not just one belonging to a Team
they actually manage.

FIX:
  - `require_task_update_access()` now scopes Team Manager access to a
    Task whose Team they actually manage (`TeamRepository.is_manager`,
    same rule `can_access_task()`'s read-side already used), and
    separately grants the Task's ACTUAL ASSIGNEE access regardless of
    role (Client explicitly excluded — never a legal assignee).
  - `update_task()` applies `ASSIGNEE_ALLOWED_TASK_UPDATE_FIELDS =
    {"status"}` to a "bare assignee" (assignee access only, no elevated
    Team/Project capability over this specific Task) — so a plain Team
    Member who merely happens to be a Task's assignee can never use this
    route to mutate assignee_id/team_id/project_id/anything else.
  - `update_task_status()` now calls the SAME canonical
    `require_task_update_access()` instead of its own bespoke
    `is_admin_or_owner`-only check — one authorization implementation,
    not two conflicting ones.

Covers (see the spec's "REGRESSION TESTS"):
  1   TM updates status of a Task assigned to themselves -> allowed (the
      exact screenshot scenario), through BOTH PATCH /tasks/{id}/status
      and PATCH /tasks/{id}.
  2   TM updates status of an unassigned Task belonging to their managed
      Team -> allowed.
  3   TM updates status of a Task assigned to a member of their managed
      Team -> allowed.
  4   TM updates a Task from an unrelated (unmanaged) Team -> 403, on
      both endpoints.
  5   TM attempts a cross-tenant Task update -> denied (404, not merely
      403 — org-scoped query, never confirms existence).
  6   TM cannot assign a wrong-Team user to their own managed-Team Task.
  7   Client cannot become a Task's assignee.
  8   The actual Team Member assignee can perform the existing
      assignee-safe status update (both endpoints), but PATCH /tasks/{id}
      still refuses any OTHER field for that same bare-assignee caller.
  9   A Team Member who is neither the manager nor the assignee cannot
      modify an unrelated Task.
  12  Working Time remains assignee-only — the managing TM who isn't the
      assignee still cannot start the timer; the actual assignee can.
  13  The active-timer assignee-change safeguard remains intact for a
      Team-Manager-initiated reassignment.
  14  Owner/Admin's existing unrestricted Task-update behavior is
      unaffected.

Items 10 (PM allowed-field update unchanged) and 11 (PM cannot directly
assign a Team Member) are already covered by
tests/test_project_manager_task_update.py and
tests/test_project_manager_task_delegation.py — both rerun and passing
unmodified by this fix, so they are not duplicated here.

Runs against the real database connection the app uses. Every row this
test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from fastapi import BackgroundTasks
from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from app.api.routes.tasks import start_task_timer, update_task, update_task_status
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import CLIENT, OWNER, TEAM_MANAGER, TEAM_MEMBER
from app.core.tenant import TenantContext
from app.models.organization import Organization, OrganizationMembership
from app.models.task import Task
from app.models.task_time_entry import TaskTimeEntry
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.schemas.task import TaskStatusUpdate, TaskUpdate


async def _run():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]

        owner = User(full_name="TMTU Owner", email=f"tmtu.owner.{suffix}@example-corp.com", hashed_password="x", role="owner")
        tm = User(full_name="TMTU TM", email=f"tmtu.tm.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MANAGER)
        tm_marketing = User(full_name="TMTU TM Marketing", email=f"tmtu.tmmkt.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MANAGER)
        bob = User(full_name="TMTU Bob", email=f"tmtu.bob.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)
        alice = User(full_name="TMTU Alice", email=f"tmtu.alice.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)
        client_user = User(full_name="TMTU Client", email=f"tmtu.client.{suffix}@example-corp.com", hashed_password="x", role=CLIENT)
        db.add_all([owner, tm, tm_marketing, bob, alice, client_user])
        await db.commit()
        for u in (owner, tm, tm_marketing, bob, alice, client_user):
            await db.refresh(u)

        org = Organization(name=f"TMTU Org {suffix}", slug=f"tmtu-org-{suffix}", owner_id=owner.id)
        other_org = Organization(name=f"TMTU Other Org {suffix}", slug=f"tmtu-other-org-{suffix}", owner_id=owner.id)
        db.add_all([org, other_org])
        await db.commit()
        org = (await db.execute(select(Organization).options(selectinload(Organization.subscription)).where(Organization.id == org.id))).scalar_one()

        memberships = {}
        for u, role in [
            (owner, OWNER), (tm, TEAM_MANAGER), (tm_marketing, TEAM_MANAGER),
            (bob, TEAM_MEMBER), (alice, TEAM_MEMBER), (client_user, CLIENT),
        ]:
            m = OrganizationMembership(organization_id=org.id, user_id=u.id, role=role)
            db.add(m)
            memberships[u.id] = m
        await db.commit()

        team_tech = Team(name=f"TMTU Tech Team {suffix}", team_manager_id=tm.id, created_by_id=owner.id, organization_id=org.id)
        team_marketing = Team(name=f"TMTU Marketing Team {suffix}", team_manager_id=tm_marketing.id, created_by_id=owner.id, organization_id=org.id)
        db.add_all([team_tech, team_marketing])
        await db.commit()
        for t in (team_tech, team_marketing):
            await db.refresh(t)
        db.add_all([
            TeamMembership(team_id=team_tech.id, user_id=tm.id),
            TeamMembership(team_id=team_tech.id, user_id=bob.id),
            TeamMembership(team_id=team_marketing.id, user_id=tm_marketing.id),
            TeamMembership(team_id=team_marketing.id, user_id=alice.id),
        ])
        await db.commit()

        # ── The exact screenshot scenario: task.assignee_id == TM's own
        # id, task.team_id == a Team TM manages, currently "todo". ────────
        task_self_assigned = Task(name=f"TMTU Self {suffix}", team_id=team_tech.id, assignee_id=tm.id, created_by_id=owner.id, organization_id=org.id, status="todo")
        task_unassigned_team = Task(name=f"TMTU Unassigned {suffix}", team_id=team_tech.id, created_by_id=owner.id, organization_id=org.id, status="todo")
        task_team_member_assigned = Task(name=f"TMTU Bob Assigned {suffix}", team_id=team_tech.id, assignee_id=bob.id, created_by_id=owner.id, organization_id=org.id, status="todo")
        task_marketing = Task(name=f"TMTU Marketing {suffix}", team_id=team_marketing.id, created_by_id=owner.id, organization_id=org.id, status="todo")
        task_cross_tenant = Task(name=f"TMTU Cross Tenant {suffix}", created_by_id=owner.id, organization_id=other_org.id, status="todo")
        task_bob_own = Task(name=f"TMTU Bob Own {suffix}", team_id=team_tech.id, assignee_id=bob.id, created_by_id=owner.id, organization_id=org.id, status="todo")
        task_unrelated_for_bob = Task(name=f"TMTU Unrelated For Bob {suffix}", team_id=team_marketing.id, assignee_id=alice.id, created_by_id=owner.id, organization_id=org.id, status="todo")
        db.add_all([
            task_self_assigned, task_unassigned_team, task_team_member_assigned,
            task_marketing, task_cross_tenant, task_bob_own, task_unrelated_for_bob,
        ])
        await db.commit()
        for t in (task_self_assigned, task_unassigned_team, task_team_member_assigned, task_marketing, task_cross_tenant, task_bob_own, task_unrelated_for_bob):
            await db.refresh(t)
        all_task_ids = [t.id for t in (task_self_assigned, task_unassigned_team, task_team_member_assigned, task_marketing, task_cross_tenant, task_bob_own, task_unrelated_for_bob)]

        owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[owner.id], user=owner, db=db)
        tm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[tm.id], user=tm, db=db)
        bob_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[bob.id], user=bob, db=db)

        async def _update(task_id, payload, tenant):
            return await update_task(task_id, payload, background_tasks=BackgroundTasks(), tenant=tenant, db=db)

        async def _update_status(task_id, status, tenant):
            return await update_task_status(task_id, TaskStatusUpdate(status=status), background_tasks=BackgroundTasks(), tenant=tenant, db=db)

        try:
            # ── 1. TM updates status of their own assigned Task — the
            # exact screenshot bug — through BOTH endpoints. ────────────────
            updated = await _update_status(task_self_assigned.id, "in_progress", tm_tenant)
            assert updated.status == "in_progress", "TM must be able to change the status of their own assigned Task via PATCH /tasks/{id}/status"
            updated2 = await _update(task_self_assigned.id, TaskUpdate(status="todo"), tm_tenant)
            assert updated2.status == "todo", "TM must be able to change the status of their own assigned Task via PATCH /tasks/{id}"

            # ── 2. TM updates status of an unassigned Task in their
            # managed Team. ─────────────────────────────────────────────
            updated = await _update_status(task_unassigned_team.id, "in_progress", tm_tenant)
            assert updated.status == "in_progress"

            # ── 3. TM updates status of a Task assigned to a member of
            # their managed Team. ───────────────────────────────────────
            updated = await _update_status(task_team_member_assigned.id, "in_progress", tm_tenant)
            assert updated.status == "in_progress"

            # ── 4. TM cannot touch a Task from an unrelated (unmanaged)
            # Team, on either endpoint. ─────────────────────────────────
            try:
                await _update_status(task_marketing.id, "in_progress", tm_tenant)
                raise AssertionError("TM must not update a Task belonging to a Team they don't manage (status endpoint)")
            except Exception as exc:
                assert getattr(exc, "status_code", None) == 403, exc
            try:
                await _update(task_marketing.id, TaskUpdate(priority="high"), tm_tenant)
                raise AssertionError("TM must not update a Task belonging to a Team they don't manage (generic endpoint)")
            except Exception as exc:
                assert getattr(exc, "status_code", None) == 403, exc

            # ── 5. Cross-tenant Task is not found at all, not merely
            # forbidden. ─────────────────────────────────────────────────
            try:
                await _update_status(task_cross_tenant.id, "in_progress", tm_tenant)
                raise AssertionError("a cross-tenant task must not be reachable at all")
            except AppException as exc:
                assert exc.status_code == 404, exc

            # ── 6. TM cannot assign a wrong-Team user (alice, on
            # marketing) to a task_tech Task. ──────────────────────────────
            try:
                await _update(task_unassigned_team.id, TaskUpdate(assignee_id=alice.id), tm_tenant)
                raise AssertionError("a user outside this exact Team must never be assignable to its Task")
            except AppException as exc:
                assert exc.code == "ASSIGNEE_NOT_TEAM_MEMBER", exc

            # ── 7. Client can never become a Task's assignee. ────────────
            try:
                await _update(task_unassigned_team.id, TaskUpdate(assignee_id=client_user.id), tm_tenant)
                raise AssertionError("a Client must never be assignable")
            except AppException as exc:
                assert exc.code == "CLIENT_NOT_ASSIGNABLE", exc

            # ── 8. The actual Team Member assignee can perform the
            # existing assignee-safe status update, on both endpoints —
            # but PATCH /tasks/{id} still refuses any OTHER field for
            # that same bare-assignee caller (never assignee_id/team_id/
            # project_id/etc., regardless of route). ────────────────────
            bob_updated = await _update_status(task_bob_own.id, "in_progress", bob_tenant)
            assert bob_updated.status == "in_progress"
            bob_updated2 = await _update(task_bob_own.id, TaskUpdate(status="todo"), bob_tenant)
            assert bob_updated2.status == "todo", "the actual Team Member assignee must be able to change status via PATCH /tasks/{id} too"
            try:
                await _update(task_bob_own.id, TaskUpdate(priority="high"), bob_tenant)
                raise AssertionError("a bare assignee (no elevated Team/Project capability) must not be able to change priority")
            except AppException as exc:
                assert exc.code == "ASSIGNEE_TASK_FIELD_FORBIDDEN", exc
            try:
                await _update(task_bob_own.id, TaskUpdate(assignee_id=bob.id), bob_tenant)
                raise AssertionError("a bare assignee must never be able to touch assignee_id, even re-sending their own id")
            except AppException as exc:
                assert exc.code == "ASSIGNEE_TASK_FIELD_FORBIDDEN", exc

            # ── 9. A Team Member who is neither the manager nor the
            # assignee cannot modify an unrelated Task. ──────────────────
            try:
                await _update(task_unrelated_for_bob.id, TaskUpdate(priority="high"), bob_tenant)
                raise AssertionError("bob is not the assignee, not a manager of team_marketing — must be denied")
            except Exception as exc:
                assert getattr(exc, "status_code", None) == 403, exc
            try:
                await _update_status(task_unrelated_for_bob.id, "in_progress", bob_tenant)
                raise AssertionError("bob is not the assignee of task_unrelated_for_bob — status endpoint must deny too")
            except Exception as exc:
                assert getattr(exc, "status_code", None) == 403, exc

            # ── 12. Working Time remains assignee-only: the managing TM
            # (not personally the assignee of task_team_member_assigned,
            # which is bob) cannot start its timer; bob can. ──────────────
            try:
                await start_task_timer(task_team_member_assigned.id, tenant=tm_tenant)
                raise AssertionError("a managing Team Manager who is not the assignee must not control the timer")
            except AppException as exc:
                assert exc.code == "TASK_TIMER_ASSIGNEE_ONLY", exc
            start_state = await start_task_timer(task_team_member_assigned.id, tenant=bob_tenant)
            assert start_state.is_active is True
            from app.api.routes.tasks import stop_task_timer
            await stop_task_timer(task_team_member_assigned.id, tenant=bob_tenant)

            # ── 13. Active-timer assignee-change safeguard remains
            # intact for a Team-Manager-initiated reassignment. ───────────
            await start_task_timer(task_team_member_assigned.id, tenant=bob_tenant)
            try:
                await _update(task_team_member_assigned.id, TaskUpdate(assignee_id=tm.id), tm_tenant)
                raise AssertionError("reassigning a Task with an active timer belonging to the current assignee must be rejected")
            except AppException as exc:
                assert exc.code == "TASK_TIMER_ACTIVE", exc
            await stop_task_timer(task_team_member_assigned.id, tenant=bob_tenant)
            # Now that the timer is stopped, the same reassignment succeeds
            # (existing, unchanged behavior).
            reassigned = await _update(task_team_member_assigned.id, TaskUpdate(assignee_id=tm.id), tm_tenant)
            assert reassigned.assignee_id == tm.id

            # ── 14. Owner/Admin existing unrestricted behavior unaffected —
            # Owner can update the unrelated marketing Task freely. ────────
            owner_updated = await _update(task_marketing.id, TaskUpdate(priority="high"), owner_tenant)
            assert owner_updated.priority == "high"
            owner_updated_status = await _update_status(task_marketing.id, "done", owner_tenant)
            assert owner_updated_status.status == "done"

        finally:
            await db.execute(delete(TaskTimeEntry).where(TaskTimeEntry.task_id.in_(all_task_ids)))
            await db.execute(delete(Task).where(Task.id.in_(all_task_ids)))
            await db.execute(delete(TeamMembership).where(TeamMembership.team_id.in_([team_tech.id, team_marketing.id])))
            await db.execute(delete(Team).where(Team.id.in_([team_tech.id, team_marketing.id])))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_([org.id, other_org.id])))
            await db.execute(delete(Organization).where(Organization.id.in_([org.id, other_org.id])))
            await db.execute(delete(User).where(User.id.in_([owner.id, tm.id, tm_marketing.id, bob.id, alice.id, client_user.id])))
            await db.commit()

    await engine.dispose()


def test_team_manager_task_update_scope():
    asyncio.run(_run())
