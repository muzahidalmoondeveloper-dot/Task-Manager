"""Regression tests for the Team Manager Delete/Approve/Assign-Back
organization-wide-escalation fix (app.api.routes.tasks.delete_task,
approve_task, assign_task_back, require_task_manage_access).

ROOT CAUSE (discovered during the Team Manager authority-precedence
audit, not the originally reported symptom): these three routes were
gated on the bare `require_org_manager` dependency — Owner, Admin, OR ANY
Team Manager, completely unscoped to which Team the Task actually
belongs to. A Team Manager of "Technology" could delete, approve, or
assign back a Task belonging to "Marketing", a Team they have no
authority over at all — the exact "if is_team_manager: allow_all_tasks"
organization-wide-escalation anti-pattern the product rules forbid.

FIX: `require_task_manage_access()` — Owner/Admin unconditional; a Team
Manager ONLY for a Task belonging to a Team they actually manage
(`TeamRepository.is_manager`), mirroring `require_task_update_access`'s
own managed-Team scope. Never available to a plain Project Manager or a
bare assignee (unchanged — these three actions were never part of
either's capability set).

Covers:
  1. TM deletes a Task belonging to their managed Team -> allowed.
  2. TM deletes a Task belonging to an unmanaged Team -> 403.
  3. TM approves a pending_review Task in their managed Team -> allowed.
  4. TM approves a pending_review Task in an unmanaged Team -> 403.
  5. TM assigns back a pending_review Task in their managed Team -> allowed.
  6. TM assigns back a pending_review Task in an unmanaged Team -> 403.
  7. Being the Task's assignee alone does NOT grant delete/approve/
     assign-back authority (these stay manager-only, unlike PATCH).
  8. Owner/Admin's existing unrestricted delete/approve/assign-back
     behavior remains unaffected.

Runs against the real database connection the app uses. Every row this
test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from fastapi import BackgroundTasks
from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from app.api.routes.tasks import approve_task, assign_task_back, delete_task
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import OWNER, TEAM_MANAGER, TEAM_MEMBER
from app.core.tenant import TenantContext
from app.models.organization import Organization, OrganizationMembership
from app.models.task import Task
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.schemas.task import AssignBackRequest


async def _run():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]

        owner = User(full_name="TMA Owner", email=f"tma.owner.{suffix}@example-corp.com", hashed_password="x", role="owner")
        tm_tech = User(full_name="TMA TM Tech", email=f"tma.tmtech.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MANAGER)
        tm_marketing = User(full_name="TMA TM Marketing", email=f"tma.tmmkt.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MANAGER)
        bob = User(full_name="TMA Bob", email=f"tma.bob.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)
        db.add_all([owner, tm_tech, tm_marketing, bob])
        await db.commit()
        for u in (owner, tm_tech, tm_marketing, bob):
            await db.refresh(u)

        org = Organization(name=f"TMA Org {suffix}", slug=f"tma-org-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.commit()
        org = (await db.execute(select(Organization).options(selectinload(Organization.subscription)).where(Organization.id == org.id))).scalar_one()

        memberships = {}
        for u, role in [(owner, OWNER), (tm_tech, TEAM_MANAGER), (tm_marketing, TEAM_MANAGER), (bob, TEAM_MEMBER)]:
            m = OrganizationMembership(organization_id=org.id, user_id=u.id, role=role)
            db.add(m)
            memberships[u.id] = m
        await db.commit()

        team_tech = Team(name=f"TMA Tech {suffix}", team_manager_id=tm_tech.id, created_by_id=owner.id, organization_id=org.id)
        team_marketing = Team(name=f"TMA Marketing {suffix}", team_manager_id=tm_marketing.id, created_by_id=owner.id, organization_id=org.id)
        db.add_all([team_tech, team_marketing])
        await db.commit()
        for t in (team_tech, team_marketing):
            await db.refresh(t)
        db.add(TeamMembership(team_id=team_tech.id, user_id=bob.id))
        await db.commit()

        owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[owner.id], user=owner, db=db)
        tm_tech_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[tm_tech.id], user=tm_tech, db=db)

        created_task_ids: list[int] = []

        async def _new_task(*, team_id, assignee_id=None, status="todo") -> Task:
            task = Task(name=f"TMA Task {uuid.uuid4().hex[:6]}", team_id=team_id, assignee_id=assignee_id, created_by_id=owner.id, organization_id=org.id, status=status)
            db.add(task)
            await db.commit()
            await db.refresh(task)
            created_task_ids.append(task.id)
            return task

        try:
            # ── 1. TM deletes a Task in their own managed Team. ──────────────
            task_own = await _new_task(team_id=team_tech.id)
            await delete_task(task_own.id, tenant=tm_tech_tenant)
            deleted_check = (await db.execute(select(Task).where(Task.id == task_own.id))).scalar_one_or_none()
            assert deleted_check is None
            created_task_ids.remove(task_own.id)

            # ── 2. TM cannot delete a Task in an unmanaged Team. ─────────────
            task_marketing = await _new_task(team_id=team_marketing.id)
            try:
                await delete_task(task_marketing.id, tenant=tm_tech_tenant)
                raise AssertionError("TM must not delete a Task belonging to a Team they don't manage")
            except Exception as exc:
                assert getattr(exc, "status_code", None) == 403, exc
            still_there = (await db.execute(select(Task).where(Task.id == task_marketing.id))).scalar_one_or_none()
            assert still_there is not None, "a rejected delete must never actually remove the row"

            # ── 3. TM approves a pending_review Task in their managed Team. ──
            task_review_own = await _new_task(team_id=team_tech.id, assignee_id=bob.id, status="pending_review")
            approved = await approve_task(task_review_own.id, background_tasks=BackgroundTasks(), tenant=tm_tech_tenant, db=db)
            assert approved.status == "done"

            # ── 4. TM cannot approve a pending_review Task in an unmanaged
            # Team. ───────────────────────────────────────────────────────
            task_review_marketing = await _new_task(team_id=team_marketing.id, status="pending_review")
            try:
                await approve_task(task_review_marketing.id, background_tasks=BackgroundTasks(), tenant=tm_tech_tenant, db=db)
                raise AssertionError("TM must not approve a Task belonging to a Team they don't manage")
            except Exception as exc:
                assert getattr(exc, "status_code", None) == 403, exc

            # ── 5. TM assigns back a pending_review Task in their managed
            # Team. ───────────────────────────────────────────────────────
            task_assignback_own = await _new_task(team_id=team_tech.id, assignee_id=bob.id, status="pending_review")
            assigned_back = await assign_task_back(task_assignback_own.id, AssignBackRequest(note="needs more work"), background_tasks=BackgroundTasks(), tenant=tm_tech_tenant, db=db)
            assert assigned_back.status == "in_progress"

            # ── 6. TM cannot assign back a pending_review Task in an
            # unmanaged Team. ─────────────────────────────────────────────
            task_assignback_marketing = await _new_task(team_id=team_marketing.id, status="pending_review")
            try:
                await assign_task_back(task_assignback_marketing.id, AssignBackRequest(), background_tasks=BackgroundTasks(), tenant=tm_tech_tenant, db=db)
                raise AssertionError("TM must not assign back a Task belonging to a Team they don't manage")
            except Exception as exc:
                assert getattr(exc, "status_code", None) == 403, exc

            # ── 7. Being the assignee alone does not grant delete/approve/
            # assign-back — tm_tech is not team_marketing's manager even
            # when personally assigned its Task. ─────────────────────────
            task_marketing_assigned_to_tm_tech = await _new_task(team_id=team_marketing.id, assignee_id=tm_tech.id, status="pending_review")
            try:
                await delete_task(task_marketing_assigned_to_tm_tech.id, tenant=tm_tech_tenant)
                raise AssertionError("being merely the assignee must never grant delete authority")
            except Exception as exc:
                assert getattr(exc, "status_code", None) == 403, exc
            try:
                await approve_task(task_marketing_assigned_to_tm_tech.id, background_tasks=BackgroundTasks(), tenant=tm_tech_tenant, db=db)
                raise AssertionError("being merely the assignee must never grant approve authority")
            except Exception as exc:
                assert getattr(exc, "status_code", None) == 403, exc

            # ── 8. Owner/Admin existing unrestricted behavior unaffected. ────
            task_for_owner = await _new_task(team_id=team_marketing.id, status="pending_review")
            owner_approved = await approve_task(task_for_owner.id, background_tasks=BackgroundTasks(), tenant=owner_tenant, db=db)
            assert owner_approved.status == "done"
            task_for_owner_delete = await _new_task(team_id=team_marketing.id)
            await delete_task(task_for_owner_delete.id, tenant=owner_tenant)
            owner_deleted_check = (await db.execute(select(Task).where(Task.id == task_for_owner_delete.id))).scalar_one_or_none()
            assert owner_deleted_check is None
            created_task_ids.remove(task_for_owner_delete.id)

        finally:
            if created_task_ids:
                await db.execute(delete(Task).where(Task.id.in_(created_task_ids)))
            await db.execute(delete(TeamMembership).where(TeamMembership.team_id.in_([team_tech.id, team_marketing.id])))
            await db.execute(delete(Team).where(Team.id.in_([team_tech.id, team_marketing.id])))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id == org.id))
            await db.execute(delete(User).where(User.id.in_([owner.id, tm_tech.id, tm_marketing.id, bob.id])))
            await db.commit()

    await engine.dispose()


def test_team_manager_task_manage_actions_scope():
    asyncio.run(_run())
