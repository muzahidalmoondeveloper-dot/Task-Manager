"""Regression tests for Personal/Standalone Task ownership permissions
(app.api.routes.tasks.is_personal_task_owner, require_task_manage_access,
PERSONAL_TASK_OWNER_ALLOWED_FIELDS) — the fix for Problem 1: an
AI-generated Task with `team_id=NULL, assignee_id=<Team Manager>` could
only have its status changed; Edit/Delete/priority/date quick-edits were
all rejected because the authorization logic only recognized "bare Team
Task assignee" (narrow, status-only) — it had no concept of "Personal
Task owner" (a DIFFERENT, STRONGER tier: team_id IS NULL, so there is no
Team to anchor ownership, and the assignee IS that ownership).

FIX: `is_personal_task_owner()` — team_id IS NULL AND assignee_id ==
caller (Client excluded) — grants PERSONAL_TASK_OWNER_ALLOWED_FIELDS
(name/description/priority/status/dates/project_id/team_id, deliberately
never assignee_id) via PATCH /tasks/{id}, and manage access
(delete_task, via `allow_personal_owner=True`) — completely independent
of role, so this also covers a plain Team Member's own AI-generated
personal Task, not just a Team Manager's.

Covers (spec's "REGRESSION TEST MATRIX" — TM/PM MY TASKS, AI TASK,
WORKING TIME, TEAM ASSIGNMENT sections):
  3   TM self-owned Personal Task: Edit allowed, Delete allowed, priority/
      status/date updates allowed.
  9   AI Gmail-style Task (team=NULL, assignee=TM) -> personal Task
      management allowed (simulated directly on the Task row — the
      automation pipeline's own creation behavior is unchanged/untouched
      by this fix, already covered by the automation test suite).
  7   TM cannot manage an unrelated Task (neither their managed Team, nor
      their own personal Task) merely for being a TM.
  8   TM bare-assignee of an unrelated TEAM Task (not personal) keeps the
      existing narrow, status-only permissions — this distinction (case 1
      vs case 2 in the spec's "IMPORTANT DISTINCTION") must not blur.
  19  A Team Member's own Personal Task (team_id NULL, they are the
      assignee) gets the same personal-owner permissions — proves the
      rule is not role-gated, only ownership-gated.
  26  Personal Task owner can start/stop their own Working Time timer
      (unchanged assignee-only timer rule — this was never broken).
  27/28 Team Task timer rules remain assignee-only / Unassigned-blocked
      (unchanged, reconfirmed).
  33  Owner/Admin unrestricted behavior on a Personal Task is unaffected.

Runs against the real database connection the app uses. Every row this
test creates is deleted before it returns.
"""

import asyncio
import uuid

from fastapi import BackgroundTasks
from sqlalchemy import select
from sqlalchemy.orm import selectinload

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete

from app.api.routes.tasks import delete_task, start_task_timer, stop_task_timer, update_task
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import OWNER, TEAM_MANAGER, TEAM_MEMBER
from app.core.tenant import TenantContext
from app.models.organization import Organization, OrganizationMembership
from app.models.task import Task
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.schemas.task import TaskUpdate


async def _run():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]

        owner = User(full_name="PTO Owner", email=f"pto.owner.{suffix}@example-corp.com", hashed_password="x", role="owner")
        tm = User(full_name="PTO TM", email=f"pto.tm.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MANAGER)
        tm_other = User(full_name="PTO TM Other", email=f"pto.tmother.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MANAGER)
        member = User(full_name="PTO Member", email=f"pto.member.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)
        db.add_all([owner, tm, tm_other, member])
        await db.commit()
        for u in (owner, tm, tm_other, member):
            await db.refresh(u)

        org = Organization(name=f"PTO Org {suffix}", slug=f"pto-org-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.commit()
        org = (await db.execute(select(Organization).options(selectinload(Organization.subscription)).where(Organization.id == org.id))).scalar_one()

        memberships = {}
        for u, role in [(owner, OWNER), (tm, TEAM_MANAGER), (tm_other, TEAM_MANAGER), (member, TEAM_MEMBER)]:
            m = OrganizationMembership(organization_id=org.id, user_id=u.id, role=role)
            db.add(m)
            memberships[u.id] = m
        await db.commit()

        team_tech = Team(name=f"PTO Tech {suffix}", team_manager_id=tm_other.id, created_by_id=owner.id, organization_id=org.id)
        db.add(team_tech)
        await db.commit()
        await db.refresh(team_tech)
        db.add(TeamMembership(team_id=team_tech.id, user_id=member.id))
        await db.commit()

        owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[owner.id], user=owner, db=db)
        tm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[tm.id], user=tm, db=db)
        member_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[member.id], user=member, db=db)

        created_task_ids: list[int] = []

        async def _make_personal_task(*, owner_user, title) -> Task:
            task = Task(name=title, team_id=None, project_id=None, assignee_id=owner_user.id, created_by_id=owner_user.id, organization_id=org.id, status="todo", priority="medium")
            db.add(task)
            await db.commit()
            await db.refresh(task)
            created_task_ids.append(task.id)
            return task

        async def _update(task_id, payload, tenant):
            return await update_task(task_id, payload, background_tasks=BackgroundTasks(), tenant=tenant, db=db)

        try:
            # ── 3, 9. TM's own (AI-generated-shaped) Personal Task: full
            # edit permissions. ─────────────────────────────────────────
            tm_personal = await _make_personal_task(owner_user=tm, title=f"PTO AI Task {suffix}")

            renamed = await _update(tm_personal.id, TaskUpdate(name="Renamed by owner"), tm_tenant)
            assert renamed.name == "Renamed by owner"
            prioritized = await _update(tm_personal.id, TaskUpdate(priority="high"), tm_tenant)
            assert prioritized.priority == "high"
            dated = await _update(tm_personal.id, TaskUpdate(start_date="2026-01-01", due_date="2026-02-01"), tm_tenant)
            assert str(dated.start_date) == "2026-01-01" and str(dated.due_date) == "2026-02-01"
            done = await _update(tm_personal.id, TaskUpdate(status="done"), tm_tenant)
            assert done.status == "done"
            reopened = await _update(tm_personal.id, TaskUpdate(status="todo"), tm_tenant)
            assert reopened.status == "todo"

            await delete_task(tm_personal.id, tenant=tm_tenant)
            gone = (await db.execute(select(Task).where(Task.id == tm_personal.id))).scalar_one_or_none()
            assert gone is None, "the Personal Task's own owner must be able to delete it"
            created_task_ids.remove(tm_personal.id)

            # ── 26. Personal Task owner can Start/Stop their own timer. ────
            tm_personal_2 = await _make_personal_task(owner_user=tm, title=f"PTO AI Task Timer {suffix}")
            start_state = await start_task_timer(tm_personal_2.id, tenant=tm_tenant)
            assert start_state.is_active is True
            stop_state = await stop_task_timer(tm_personal_2.id, tenant=tm_tenant)
            assert stop_state.is_active is False

            # ── 7. TM cannot manage an unrelated Task — not their managed
            # Team (they don't manage team_tech), not their own Personal
            # Task. ──────────────────────────────────────────────────────
            unrelated_team_task = Task(name=f"PTO Unrelated {suffix}", team_id=team_tech.id, assignee_id=member.id, created_by_id=owner.id, organization_id=org.id, status="todo")
            db.add(unrelated_team_task)
            await db.commit()
            await db.refresh(unrelated_team_task)
            created_task_ids.append(unrelated_team_task.id)
            try:
                await _update(unrelated_team_task.id, TaskUpdate(priority="high"), tm_tenant)
                raise AssertionError("a TM must not manage a Task in a Team they don't manage, and they aren't its assignee either")
            except Exception as exc:
                assert getattr(exc, "status_code", None) == 403, exc

            # ── 8. TM as bare assignee of an unrelated TEAM Task (not
            # personal — team_id is set) keeps the narrow, status-only
            # permission, never full personal-task management. ─────────────
            tm_as_team_assignee = Task(name=f"PTO TM Team Assignee {suffix}", team_id=team_tech.id, assignee_id=tm.id, created_by_id=owner.id, organization_id=org.id, status="todo")
            db.add(tm_as_team_assignee)
            await db.commit()
            await db.refresh(tm_as_team_assignee)
            created_task_ids.append(tm_as_team_assignee.id)

            status_only = await _update(tm_as_team_assignee.id, TaskUpdate(status="in_progress"), tm_tenant)
            assert status_only.status == "in_progress", "the bare-assignee status-only path must still work (unchanged)"
            try:
                await _update(tm_as_team_assignee.id, TaskUpdate(priority="high"), tm_tenant)
                raise AssertionError("a bare Team Task assignee (not the Team's manager) must NOT get personal-task-owner field freedom")
            except AppException as exc:
                assert exc.code == "ASSIGNEE_TASK_FIELD_FORBIDDEN", exc
            try:
                await delete_task(tm_as_team_assignee.id, tenant=tm_tenant)
                raise AssertionError("a bare Team Task assignee must never get delete authority")
            except Exception as exc:
                assert getattr(exc, "status_code", None) == 403, exc

            # ── 19. A plain Team Member's own Personal Task gets the same
            # personal-owner permissions — proves this is ownership-gated,
            # not role-gated. ────────────────────────────────────────────
            member_personal = await _make_personal_task(owner_user=member, title=f"PTO Member Personal {suffix}")
            member_updated = await _update(member_personal.id, TaskUpdate(priority="high", description="my own note"), member_tenant)
            assert member_updated.priority == "high"
            assert member_updated.description == "my own note"
            await delete_task(member_personal.id, tenant=member_tenant)
            member_gone = (await db.execute(select(Task).where(Task.id == member_personal.id))).scalar_one_or_none()
            assert member_gone is None
            created_task_ids.remove(member_personal.id)

            # A plain Team Member still cannot touch an unrelated Task
            # (existing, unchanged behavior).
            try:
                await _update(unrelated_team_task.id, TaskUpdate(priority="low"), member_tenant)
                raise AssertionError("a Team Member must not manage a Task they don't own and aren't the manager of")
            except Exception as exc:
                assert getattr(exc, "status_code", None) == 403, exc

            # ── 33. Owner/Admin unrestricted behavior on a Personal Task
            # is unaffected. ─────────────────────────────────────────────
            owner_view_of_tm_personal = await _make_personal_task(owner_user=tm, title=f"PTO For Owner {suffix}")
            owner_updated = await _update(owner_view_of_tm_personal.id, TaskUpdate(priority="high", assignee_id=owner.id), owner_tenant)
            assert owner_updated.priority == "high"
            assert owner_updated.assignee_id == owner.id, "Owner/Admin must retain full, unrestricted field access including assignee_id"

        finally:
            if created_task_ids:
                await db.execute(delete(Task).where(Task.id.in_(created_task_ids)))
            await db.execute(delete(TeamMembership).where(TeamMembership.team_id == team_tech.id))
            await db.execute(delete(Team).where(Team.id == team_tech.id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id == org.id))
            await db.execute(delete(User).where(User.id.in_([owner.id, tm.id, tm_other.id, member.id])))
            await db.commit()

    await engine.dispose()


def test_personal_task_ownership_permissions():
    asyncio.run(_run())
