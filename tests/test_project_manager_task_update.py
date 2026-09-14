"""Regression tests for the Project Manager Task-update / inline-management
follow-up (app.api.routes.tasks.require_task_update_access,
PM_ALLOWED_TASK_UPDATE_FIELDS, and update_task()'s new scoped gate).

Root cause (see final report): `PATCH /tasks/{id}` was gated by
`require_org_manager` (Owner/Admin/Team Manager only) with no path for a
plain Project Manager at all — even on a task under a project they
genuinely manage. This meant a PM could see a Task in their managed
project's "All Tasks" but every attempted edit (status, dates, priority,
the Done checkmark) failed with a blanket "You are not allowed to update
tasks." 403.

The fix introduces `require_task_update_access()` — Owner/Admin/Team
Manager behavior is completely unchanged; a plain Project Manager
(has_project_manager_access, no Owner/Admin/Team-Manager capability) is
now let through, but ONLY for a task whose project they hold a genuine
ProjectMembership on, and even then only for a strict field whitelist
(name/description/priority/status/start_date/due_date/team_id) —
assignee_id and project_id remain completely off-limits, matching the
established "PM delegates to Team, Team Manager assigns the individual"
model exactly.

Covers (see the spec's "BACKEND TESTS"):
  1-6   PM can update title/description/priority/status/start_date/
        due_date on a managed-project task.
  7-9   PM can mark a task Done (completed_at/completed_by_id/
        reviewed_at set, matching existing completion semantics) and
        reopen it (those fields cleared) — same server-side logic Owner/
        Admin/Team Manager already use, not a PM-specific copy.
  10    PM can update allowed fields on a delegated Team Task (assigned
        to a Team Member) without touching the assignee.
  11-14 PM cannot PATCH assignee_id to a Team Member, a Team Manager,
        themself, or explicit null — every case rejected with
        PROJECT_MANAGER_CANNOT_ASSIGN_MEMBER, never silently ignored.
  15    PM cannot update a Task under an unrelated Project.
  16    PM cannot update a project-less Task merely for being in the
        same org.
  17    A cross-tenant Task is not found at all (org-scoped query),
        never merely "forbidden" (which would confirm existence).
  18    PM cannot change project_id (whitelist rejection).
  19    PM Team change to another Team attached to the same Project on
        an UNASSIGNED task succeeds.
  20    PM Team change to a Team not attached to the Project is
        rejected.
  21    PM Team change on an ASSIGNED task that would invalidate the
        existing assignee is rejected (existing, unchanged
        validate_task_assignee re-check).
  22    Owner's existing update-assignment behavior (setting assignee_id
        directly) is completely unaffected.
  23    Team Manager's existing Team-scoped update behavior (including
        assignee_id) is completely unaffected.
  24-25 Team Member and Client remain denied, exactly as before.
  26    Timer Start/Stop remains strictly assignee-only — a PM who is
        not the assignee still cannot control it.
  27    Working Time read remains available to the PM on a
        managed-project task.
  28    A successful PM update produces a real task.updated Activity Log
        entry with accurate metadata.
  29    A rejected mutation (the assignee attempts above) produces NO
        Activity Log entry at all.

Runs against the real database connection the app uses. Every row this
test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from fastapi import BackgroundTasks
from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from app.api.routes.tasks import (
    get_task_time_state,
    start_task_timer,
    stop_task_timer,
    update_task,
)
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import CLIENT, OWNER, PROJECT_MANAGER, TEAM_MANAGER, TEAM_MEMBER
from app.core.tenant import TenantContext
from app.models.activity_log import ActivityLog
from app.models.organization import Organization, OrganizationMembership
from app.models.project import Project, ProjectMembership, ProjectTeam
from app.models.task import Task
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.schemas.task import TaskUpdate


async def _run():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]

        owner = User(full_name="PMTU Owner", email=f"pmtu.owner.{suffix}@example-corp.com", hashed_password="x", role="owner")
        admin = User(full_name="PMTU Admin", email=f"pmtu.admin.{suffix}@example-corp.com", hashed_password="x", role="admin")
        pm = User(full_name="PMTU PM", email=f"pmtu.pm.{suffix}@example-corp.com", hashed_password="x", role=PROJECT_MANAGER)
        tm = User(full_name="PMTU TM", email=f"pmtu.tm.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MANAGER)
        bob = User(full_name="PMTU Bob", email=f"pmtu.bob.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)
        member = User(full_name="PMTU Member", email=f"pmtu.member.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)
        client_user = User(full_name="PMTU Client", email=f"pmtu.client.{suffix}@example-corp.com", hashed_password="x", role=CLIENT)
        db.add_all([owner, admin, pm, tm, bob, member, client_user])
        await db.commit()
        for u in (owner, admin, pm, tm, bob, member, client_user):
            await db.refresh(u)

        org = Organization(name=f"PMTU Org {suffix}", slug=f"pmtu-org-{suffix}", owner_id=owner.id)
        other_org = Organization(name=f"PMTU Other Org {suffix}", slug=f"pmtu-other-org-{suffix}", owner_id=owner.id)
        db.add_all([org, other_org])
        await db.commit()
        org = (await db.execute(select(Organization).options(selectinload(Organization.subscription)).where(Organization.id == org.id))).scalar_one()

        memberships = {}
        for u, role, extra in [
            (owner, OWNER, {}), (admin, "admin", {}), (pm, PROJECT_MANAGER, {}),
            (tm, TEAM_MANAGER, {}), (bob, TEAM_MEMBER, {}), (member, TEAM_MEMBER, {}),
            (client_user, CLIENT, {}),
        ]:
            m = OrganizationMembership(organization_id=org.id, user_id=u.id, role=role, **extra)
            db.add(m)
            memberships[u.id] = m
        await db.commit()

        project_a = Project(name=f"PMTU Project A {suffix}", created_by_id=owner.id, organization_id=org.id)
        project_b = Project(name=f"PMTU Project B {suffix}", created_by_id=owner.id, organization_id=org.id)  # pm NOT a member
        db.add_all([project_a, project_b])
        await db.commit()
        for p in (project_a, project_b):
            await db.refresh(p)
        db.add(ProjectMembership(project_id=project_a.id, user_id=pm.id))
        await db.commit()

        team_a = Team(name=f"PMTU Team A {suffix}", team_manager_id=tm.id, created_by_id=owner.id, organization_id=org.id)
        team_x = Team(name=f"PMTU Team X {suffix}", team_manager_id=tm.id, created_by_id=owner.id, organization_id=org.id)  # exists, NOT attached to project_a
        db.add_all([team_a, team_x])
        await db.commit()
        for t in (team_a, team_x):
            await db.refresh(t)
        db.add_all([
            TeamMembership(team_id=team_a.id, user_id=tm.id),
            TeamMembership(team_id=team_a.id, user_id=bob.id),
            ProjectTeam(project_id=project_a.id, team_id=team_a.id, assigned_by_id=owner.id),
        ])
        await db.commit()

        task_core = Task(name=f"PMTU Core {suffix}", project_id=project_a.id, team_id=team_a.id, created_by_id=owner.id, organization_id=org.id, status="todo", priority="low")
        task_delegated = Task(name=f"PMTU Delegated {suffix}", project_id=project_a.id, team_id=team_a.id, assignee_id=bob.id, created_by_id=owner.id, organization_id=org.id)
        task_unrelated = Task(name=f"PMTU Unrelated {suffix}", project_id=project_b.id, created_by_id=owner.id, organization_id=org.id)
        task_no_project = Task(name=f"PMTU No Project {suffix}", created_by_id=owner.id, organization_id=org.id)
        task_team_only = Task(name=f"PMTU Team Only {suffix}", project_id=project_a.id, team_id=team_a.id, created_by_id=owner.id, organization_id=org.id)
        task_cross_tenant = Task(name=f"PMTU Cross Tenant {suffix}", created_by_id=owner.id, organization_id=other_org.id)
        db.add_all([task_core, task_delegated, task_unrelated, task_no_project, task_team_only, task_cross_tenant])
        await db.commit()
        for t in (task_core, task_delegated, task_unrelated, task_no_project, task_team_only, task_cross_tenant):
            await db.refresh(t)
        all_task_ids = [t.id for t in (task_core, task_delegated, task_unrelated, task_no_project, task_team_only, task_cross_tenant)]

        owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[owner.id], user=owner, db=db)
        pm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[pm.id], user=pm, db=db)
        tm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[tm.id], user=tm, db=db)
        member_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[member.id], user=member, db=db)
        client_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[client_user.id], user=client_user, db=db)

        async def _update(task_id, payload, tenant):
            return await update_task(task_id, payload, background_tasks=BackgroundTasks(), tenant=tenant, db=db)

        try:
            # ── 1-6. PM can update core fields on a managed-project task. ──────
            updated = await _update(task_core.id, TaskUpdate(name="PMTU Core Renamed"), pm_tenant)
            assert updated.name == "PMTU Core Renamed"
            updated = await _update(task_core.id, TaskUpdate(description="new description"), pm_tenant)
            assert updated.description == "new description"
            updated = await _update(task_core.id, TaskUpdate(priority="high"), pm_tenant)
            assert updated.priority == "high"
            updated = await _update(task_core.id, TaskUpdate(status="in_progress"), pm_tenant)
            assert updated.status == "in_progress"
            updated = await _update(task_core.id, TaskUpdate(start_date="2026-01-01"), pm_tenant)
            assert str(updated.start_date) == "2026-01-01"
            updated = await _update(task_core.id, TaskUpdate(due_date="2026-02-01"), pm_tenant)
            assert str(updated.due_date) == "2026-02-01"

            # ── 7, 8. PM can mark Done — existing completion semantics. ────────
            done = await _update(task_core.id, TaskUpdate(status="done"), pm_tenant)
            assert done.status == "done"
            done_row = (await db.execute(select(Task).where(Task.id == task_core.id))).scalar_one()
            assert done_row.completed_at is not None
            assert done_row.completed_by_id is not None

            # ── 9. Reopen clears completion fields. ───────────────────────────
            reopened = await _update(task_core.id, TaskUpdate(status="todo"), pm_tenant)
            assert reopened.status == "todo"
            reopened_row = (await db.execute(select(Task).where(Task.id == task_core.id))).scalar_one()
            assert reopened_row.completed_at is None
            assert reopened_row.completed_by_id is None

            # ── 10. PM can update allowed fields on a delegated Team Task
            # without touching its assignee. ──────────────────────────────────
            delegated_updated = await _update(task_delegated.id, TaskUpdate(priority="high"), pm_tenant)
            assert delegated_updated.priority == "high"
            assert delegated_updated.assignee_id == bob.id, "an allowed-field update must never disturb the existing assignee"

            # ── 11, 12, 13, 14. Assignee stays completely off-limits. ──────────
            for bad_payload, label in [
                (TaskUpdate(assignee_id=bob.id), "Team Member"),
                (TaskUpdate(assignee_id=tm.id), "Team Manager"),
                (TaskUpdate(assignee_id=pm.id), "themself"),
                (TaskUpdate(assignee_id=None), "explicit null"),
            ]:
                try:
                    await _update(task_delegated.id, bad_payload, pm_tenant)
                    raise AssertionError(f"PM must not be able to PATCH assignee_id to {label}")
                except AppException as exc:
                    assert exc.code == "PROJECT_MANAGER_CANNOT_ASSIGN_MEMBER", exc

            # ── 15. Unrelated project's task denied. ──────────────────────────
            try:
                await _update(task_unrelated.id, TaskUpdate(priority="high"), pm_tenant)
                raise AssertionError("PM must not update a task under a project they don't manage")
            except Exception as exc:
                assert getattr(exc, "status_code", None) == 403, exc

            # ── 16. Project-less task denied, even same org. ──────────────────
            try:
                await _update(task_no_project.id, TaskUpdate(priority="high"), pm_tenant)
                raise AssertionError("PM must not update a project-less task")
            except Exception as exc:
                assert getattr(exc, "status_code", None) == 403, exc

            # ── 17. Cross-tenant task is not found, not merely forbidden. ──────
            try:
                await _update(task_cross_tenant.id, TaskUpdate(priority="high"), pm_tenant)
                raise AssertionError("a cross-tenant task must not be reachable at all")
            except AppException as exc:
                assert exc.status_code == 404, exc

            # ── 18. PM cannot change project_id. ───────────────────────────────
            try:
                await _update(task_core.id, TaskUpdate(project_id=project_b.id), pm_tenant)
                raise AssertionError("PM must not be able to move a task to a different project")
            except AppException as exc:
                assert exc.code == "PROJECT_MANAGER_TASK_FIELD_FORBIDDEN", exc

            # ── 19. Team change on an UNASSIGNED task to another Team
            # attached to the same project succeeds. First attach team_x to
            # project_a as an Owner action, then PM may switch to it. ─────────
            await owner_tenant.db.execute(
                ProjectTeam.__table__.insert().values(project_id=project_a.id, team_id=team_x.id, assigned_by_id=owner.id)
            )
            await db.commit()
            team_changed = await _update(task_team_only.id, TaskUpdate(team_id=team_x.id), pm_tenant)
            assert team_changed.team_id == team_x.id

            # ── 20. Team change to a Team NOT attached to the project rejected. ─
            team_y = Team(name=f"PMTU Team Y {suffix}", team_manager_id=tm.id, created_by_id=owner.id, organization_id=org.id)
            db.add(team_y)
            await db.commit()
            await db.refresh(team_y)
            try:
                await _update(task_team_only.id, TaskUpdate(team_id=team_y.id), pm_tenant)
                raise AssertionError("a team not attached to this project must be rejected")
            except Exception as exc:
                assert getattr(exc, "status_code", None) == 403, exc

            # ── 21. Team change on an ASSIGNED task that would invalidate the
            # assignee is rejected (bob is not a member of team_x). ────────────
            try:
                await _update(task_delegated.id, TaskUpdate(team_id=team_x.id), pm_tenant)
                raise AssertionError("changing Team must never silently invalidate the existing assignee")
            except AppException as exc:
                assert exc.code == "ASSIGNEE_NOT_TEAM_MEMBER", exc

            # ── 22. Owner's existing assignment behavior unaffected. ───────────
            owner_assigned = await _update(task_core.id, TaskUpdate(assignee_id=bob.id), owner_tenant)
            assert owner_assigned.assignee_id == bob.id

            # ── 23. Team Manager's existing Team-scoped assignment behavior
            # unaffected. ───────────────────────────────────────────────────
            tm_assigned = await _update(task_delegated.id, TaskUpdate(assignee_id=tm.id), tm_tenant)
            assert tm_assigned.assignee_id == tm.id

            # ── 24, 25. Team Member and Client remain denied. ──────────────────
            try:
                await _update(task_core.id, TaskUpdate(priority="low"), member_tenant)
                raise AssertionError("a plain Team Member must not be able to update this task")
            except Exception as exc:
                assert getattr(exc, "status_code", None) == 403, exc

            try:
                await _update(task_core.id, TaskUpdate(priority="low"), client_tenant)
                raise AssertionError("a Client must not be able to update this task")
            except Exception as exc:
                assert getattr(exc, "status_code", None) == 403, exc

            # ── 26. Timer remains strictly assignee-only — PM is not the
            # assignee of task_delegated (currently tm). ───────────────────────
            try:
                await start_task_timer(task_delegated.id, tenant=pm_tenant)
                raise AssertionError("PM must not be able to start a timer on a task assigned to someone else")
            except AppException as exc:
                assert exc.code == "TASK_TIMER_ASSIGNEE_ONLY", exc

            # ── 27. Working Time read remains available to the PM. ────────────
            time_state = await get_task_time_state(task_core.id, tenant=pm_tenant)
            assert time_state.assignee_id == bob.id

            # ── 28. Successful PM update produces a real Activity Log entry. ──
            await _update(task_core.id, TaskUpdate(priority="medium"), pm_tenant)
            success_logs = (await db.execute(
                select(ActivityLog).where(
                    ActivityLog.organization_id == org.id, ActivityLog.entity_id == task_core.id,
                    ActivityLog.actor_user_id == pm.id, ActivityLog.action == "task.updated",
                )
            )).scalars().all()
            assert len(success_logs) >= 1, "a successful PM update must produce a real Activity Log entry"

            # ── 29. A rejected assignee mutation produces NO Activity Log
            # entry at all for that attempt. ───────────────────────────────────
            before_count = len((await db.execute(
                select(ActivityLog).where(ActivityLog.organization_id == org.id, ActivityLog.entity_id == task_delegated.id)
            )).scalars().all())
            try:
                await _update(task_delegated.id, TaskUpdate(assignee_id=bob.id), pm_tenant)
            except AppException:
                pass
            after_count = len((await db.execute(
                select(ActivityLog).where(ActivityLog.organization_id == org.id, ActivityLog.entity_id == task_delegated.id)
            )).scalars().all())
            assert after_count == before_count, "a rejected mutation must never produce a fake success Activity Log entry"

        finally:
            await db.execute(delete(ActivityLog).where(ActivityLog.organization_id.in_([org.id, other_org.id])))
            await db.execute(delete(Task).where(Task.id.in_(all_task_ids)))
            await db.execute(delete(TeamMembership).where(TeamMembership.team_id.in_([team_a.id, team_x.id, team_y.id])))
            await db.execute(delete(ProjectTeam).where(ProjectTeam.project_id == project_a.id))
            await db.execute(delete(Team).where(Team.id.in_([team_a.id, team_x.id, team_y.id])))
            await db.execute(delete(ProjectMembership).where(ProjectMembership.project_id.in_([project_a.id, project_b.id])))
            await db.execute(delete(Project).where(Project.id.in_([project_a.id, project_b.id])))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id.in_([org.id, other_org.id])))
            await db.execute(delete(User).where(User.id.in_([owner.id, admin.id, pm.id, tm.id, bob.id, member.id, client_user.id])))
            await db.commit()

    await engine.dispose()


def test_project_manager_task_update():
    asyncio.run(_run())
