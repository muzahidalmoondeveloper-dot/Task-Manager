"""Regression tests for two related follow-up fixes:

ISSUE 1 — a plain Project Manager was blocked by `_PM_PROJECT_REQUIRED`
from creating a project-less, team-less Personal/Standalone Task
(project_id=NULL, team_id=NULL, assignee_id=self) — that invariant now
only applies when `team_id` is set (Team delegation genuinely needs a
Project to anchor its authorization scope; a PM's own Personal Task does
not). See `create_task()`'s updated `is_plain_project_manager` branch.

ISSUE 2 — the Edit Task modal used to always resend the task's full,
UNCHANGED field set on submit, including `assignee_id` at its current
value. For a Personal Task owner (team_id NULL, they ARE the assignee),
`assignee_id` is deliberately not in `PERSONAL_TASK_OWNER_ALLOWED_FIELDS`
— so even a no-op resend of their own id was rejected outright. The
actual fix is frontend-only (submit only changed fields — see
TasksPage.jsx's `buildChangedTaskFields`), but the backend contract this
relies on (assignee_id stays protected for a Personal Task owner no
matter what value is sent) is what these tests pin down.

Covers the spec's ISSUE 1 test items 1, 2 (My Tasks), 3 (All Tasks), 4
(personal-task management), 5, 6, 7, 8, 9, 10, and ISSUE 2 item 18
(explicit assignee_id PATCH from a Personal Task owner is rejected
regardless of value, including a same-value no-op).

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

from app.api.routes.tasks import create_task, delete_task, list_my_tasks, list_tasks, update_task
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import OWNER, PROJECT_MANAGER, TEAM_MANAGER
from app.core.tenant import TenantContext
from app.models.organization import Organization, OrganizationMembership
from app.models.project import Project, ProjectMembership, ProjectTeam
from app.models.task import Task
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.schemas.task import TaskCreate, TaskUpdate


async def _run():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]

        owner = User(full_name="PPE Owner", email=f"ppe.owner.{suffix}@example-corp.com", hashed_password="x", role="owner")
        pm = User(full_name="PPE PM", email=f"ppe.pm.{suffix}@example-corp.com", hashed_password="x", role=PROJECT_MANAGER)
        tm = User(full_name="PPE TM", email=f"ppe.tm.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MANAGER)
        member = User(full_name="PPE Member", email=f"ppe.member.{suffix}@example-corp.com", hashed_password="x", role="team_member")
        db.add_all([owner, pm, tm, member])
        await db.commit()
        for u in (owner, pm, tm, member):
            await db.refresh(u)

        org = Organization(name=f"PPE Org {suffix}", slug=f"ppe-org-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.commit()
        org = (await db.execute(select(Organization).options(selectinload(Organization.subscription)).where(Organization.id == org.id))).scalar_one()

        memberships = {}
        for u, role in [(owner, OWNER), (pm, PROJECT_MANAGER), (tm, TEAM_MANAGER), (member, "team_member")]:
            m = OrganizationMembership(organization_id=org.id, user_id=u.id, role=role)
            db.add(m)
            memberships[u.id] = m
        await db.commit()

        team_tech = Team(name=f"PPE Tech {suffix}", team_manager_id=tm.id, created_by_id=owner.id, organization_id=org.id)
        db.add(team_tech)
        await db.commit()
        await db.refresh(team_tech)
        db.add_all([
            TeamMembership(team_id=team_tech.id, user_id=tm.id),
            TeamMembership(team_id=team_tech.id, user_id=pm.id),
            TeamMembership(team_id=team_tech.id, user_id=member.id),
        ])
        await db.commit()

        # A Project the PM legitimately manages, with team_tech attached.
        managed_project = Project(name=f"PPE Managed {suffix}", created_by_id=owner.id, organization_id=org.id)
        db.add(managed_project)
        await db.commit()
        await db.refresh(managed_project)
        db.add(ProjectMembership(project_id=managed_project.id, user_id=pm.id))
        await db.commit()
        db.add(ProjectTeam(project_id=managed_project.id, team_id=team_tech.id, assigned_by_id=owner.id))
        await db.commit()

        # A Project the PM has NO membership on (unrelated).
        unrelated_project = Project(name=f"PPE Unrelated {suffix}", created_by_id=owner.id, organization_id=org.id)
        db.add(unrelated_project)
        await db.commit()
        await db.refresh(unrelated_project)

        owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[owner.id], user=owner, db=db)
        pm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[pm.id], user=pm, db=db)
        tm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[tm.id], user=tm, db=db)

        created_task_ids: list[int] = []

        async def _create(payload, tenant):
            task = await create_task(payload, background_tasks=BackgroundTasks(), tenant=tenant, db=db)
            created_task_ids.append(task.id)
            return task

        async def _update(task_id, payload, tenant):
            return await update_task(task_id, payload, background_tasks=BackgroundTasks(), tenant=tenant, db=db)

        try:
            # ── ISSUE 1 / A. Project-less, team-less PM Personal Task now
            # succeeds — self-assigned, no Project required. ────────────────
            pm_personal = await _create(TaskCreate(name=f"PPE PM Personal {suffix}"), pm_tenant)
            assert pm_personal.project_id is None
            assert pm_personal.team_id is None
            assert pm_personal.assignee_id == pm.id
            assert pm_personal.created_by_id == pm.id

            persisted = (await db.execute(select(Task).where(Task.id == pm_personal.id))).scalar_one()
            assert not (persisted.team_id is None and persisted.project_id is None and persisted.assignee_id is None), \
                "must never persist as a fully-orphaned Task"

            # ── 2, 3. Appears in PM My Tasks AND PM All Tasks. ──────────────
            pm_my_tasks = await list_my_tasks(
                status_filter=None, priority_filter=None, due_date_from=None, due_date_to=None,
                overdue=False, project_id=None, team_id=None, tenant=pm_tenant,
            )
            assert pm_personal.id in {t.id for t in pm_my_tasks}

            pm_all_tasks = await list_tasks(
                status_filter=None, priority_filter=None, assignee_id=None, project_id=None, team_id=None,
                due_date_from=None, due_date_to=None, overdue=False, tenant=pm_tenant,
            )
            assert pm_personal.id in {t.id for t in pm_all_tasks}

            # ── 4. PM can fully manage this Personal Task through the
            # existing personal-task-owner rules (not merely PM_ALLOWED_
            # TASK_UPDATE_FIELDS — the broader personal-owner tier). ────────
            prioritized = await _update(pm_personal.id, TaskUpdate(priority="high", description="my own note"), pm_tenant)
            assert prioritized.priority == "high"
            assert prioritized.description == "my own note"
            await delete_task(pm_personal.id, tenant=pm_tenant)
            gone = (await db.execute(select(Task).where(Task.id == pm_personal.id))).scalar_one_or_none()
            assert gone is None, "the PM's own Personal Task must be deletable by them"
            created_task_ids.remove(pm_personal.id)

            # ── 5, B. Project-linked Personal Task: project=managed Project,
            # team=NULL -> succeeds, assignee=PM. ───────────────────────────
            pm_personal_with_project = await _create(
                TaskCreate(name=f"PPE PM Personal Project {suffix}", project_id=managed_project.id), pm_tenant,
            )
            assert pm_personal_with_project.project_id == managed_project.id
            assert pm_personal_with_project.team_id is None
            assert pm_personal_with_project.assignee_id == pm.id

            # ── 6. Unrelated Project (PM has no ProjectMembership) ->
            # rejected. ──────────────────────────────────────────────────
            try:
                await _create(TaskCreate(name=f"PPE PM Unrelated {suffix}", project_id=unrelated_project.id), pm_tenant)
                raise AssertionError("a plain PM must not create a Task under a Project they don't have access to")
            except Exception as exc:
                assert getattr(exc, "status_code", None) == 403, exc

            # ── 7, D. Team without Project -> still invalid (delegation
            # authority still originates through a managed Project). ───────
            try:
                await _create(TaskCreate(name=f"PPE PM Team No Project {suffix}", team_id=team_tech.id), pm_tenant)
                raise AssertionError("a plain PM must still need a Project to delegate a Task to a Team")
            except AppException as exc:
                assert exc.code == "PROJECT_REQUIRED", exc

            # ── 8, C. PM delegates: project=managed Project, team=attached
            # Team, assignee omitted -> assignee stays NULL. ────────────────
            delegated = await _create(
                TaskCreate(name=f"PPE PM Delegated {suffix}", project_id=managed_project.id, team_id=team_tech.id), pm_tenant,
            )
            assert delegated.assignee_id is None
            assert delegated.team_id == team_tech.id
            assert delegated.project_id == managed_project.id

            # ── 10, E. PM cannot directly assign a Team Member through
            # delegation. ────────────────────────────────────────────────
            try:
                await _create(
                    TaskCreate(
                        name=f"PPE PM Delegated Bad {suffix}", project_id=managed_project.id,
                        team_id=team_tech.id, assignee_id=member.id,
                    ),
                    pm_tenant,
                )
                raise AssertionError("a plain PM must never directly assign a Team Member through Project+Team delegation")
            except AppException as exc:
                assert exc.code == "PROJECT_MANAGER_CANNOT_ASSIGN_MEMBER", exc

            # ── ISSUE 2 / item 18. A Personal Task owner (any role) who
            # explicitly includes assignee_id in their PATCH is rejected —
            # even resending their OWN current (unchanged) value. This is
            # the exact backend behavior the frontend fix (only ever
            # OMITTING this field, never resending it) must work around,
            # never weaken. ──────────────────────────────────────────────
            tm_personal = Task(
                name=f"PPE TM Personal {suffix}", team_id=None, project_id=None,
                assignee_id=tm.id, created_by_id=tm.id, organization_id=org.id, status="todo",
            )
            db.add(tm_personal)
            await db.commit()
            await db.refresh(tm_personal)
            created_task_ids.append(tm_personal.id)

            try:
                await _update(tm_personal.id, TaskUpdate(priority="high", assignee_id=tm.id), tm_tenant)
                raise AssertionError("a Personal Task owner's PATCH must reject assignee_id even as a same-value no-op")
            except AppException as exc:
                assert exc.code == "PERSONAL_TASK_FIELD_FORBIDDEN", exc

            # Confirms the field really was rejected outright, not silently
            # dropped-then-applied: priority must be unchanged too (the
            # whole PATCH is refused, not partially applied).
            reloaded = (await db.execute(select(Task).where(Task.id == tm_personal.id))).scalar_one()
            assert reloaded.priority != "high"

            # The same PATCH with assignee_id simply omitted succeeds.
            ok = await _update(tm_personal.id, TaskUpdate(priority="high"), tm_tenant)
            assert ok.priority == "high"
            assert ok.assignee_id == tm.id

        finally:
            if created_task_ids:
                await db.execute(delete(Task).where(Task.id.in_(created_task_ids)))
            await db.execute(delete(ProjectTeam).where(ProjectTeam.project_id == managed_project.id))
            await db.execute(delete(ProjectMembership).where(ProjectMembership.project_id == managed_project.id))
            await db.execute(delete(Project).where(Project.id.in_([managed_project.id, unrelated_project.id])))
            await db.execute(delete(TeamMembership).where(TeamMembership.team_id == team_tech.id))
            await db.execute(delete(Team).where(Team.id == team_tech.id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id == org.id))
            await db.execute(delete(User).where(User.id.in_([owner.id, pm.id, tm.id, member.id])))
            await db.commit()

    await engine.dispose()


def test_pm_personal_task_and_personal_owner_assignee_protection():
    asyncio.run(_run())
