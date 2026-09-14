"""Regression tests for the Task ownership / default-assignee follow-up.

ROOT CAUSE (Problem 2 — TM My Tasks orphan Task): `create_task()`'s
"team_id absent -> default assignee to the creator" rule only ever ran
for `is_plain_project_manager`. Every OTHER role reaching this route
(Owner/Admin/Team Manager) got NO default at all — a Team Manager's
"My Tasks -> Add Task" with no Team selected and the Assignee left at
"Unassigned" created `team_id=NULL, assignee_id=NULL`: an orphan Task,
invisible to My Tasks (assignee-scoped) the instant it was created.

FIX: the same default now applies whenever `payload.team_id is None and
payload.assignee_id is None`, for ANY role reaching create_task — NEVER
when team_id is set (Team-delegated Tasks always stay Unassigned unless
an eligible assignee is explicitly named, unchanged), and the existing
plain-PM branch (which already had its own, unchanged, slightly stricter
contract — rejects an explicit OTHER assignee too) is left completely
untouched.

Covers (see the spec's "REGRESSION TEST MATRIX", TM/PM MY TASKS +
TEAM ASSIGNMENT sections):
  1   TM creates with team=NULL, assignee omitted -> self-assigned,
      appears in My Tasks (via list_for_assignee).
  2   No orphan possible from a normal TM creation: team=NULL AND
      assignee=NULL never persists.
  4/5 Same self-assign default from "All Tasks" creation context (same
      route — there is no separate one) — appears in My Tasks AND in
      TM's All Tasks (the new scope_personal_owner_id union).
  6   TM creates a Team Task with no assignee -> stays Unassigned; visible
      in TM All Tasks, NOT in TM My Tasks.
  13  PM personal standalone Task (team=NULL, assignee omitted) ->
      self-assigned (existing behavior, reconfirmed unchanged).
  16  PM Project+Team delegated Task -> assignee stays NULL (unchanged).
  17  PM never becomes assignee merely for creating a Team-delegated Task
      (explicit non-self assignee_id still rejected, unchanged).
  22  Team Task Unassigned remains a valid, persisted state generally
      (Owner/Admin explicit creation too).
  33  Owner/Admin explicit-assignee creation behavior is completely
      unaffected (this fix only fills in what was OMITTED).

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

from app.api.routes.tasks import create_task, list_my_tasks, list_tasks
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import OWNER, PROJECT_MANAGER, TEAM_MANAGER
from app.core.tenant import TenantContext
from app.models.organization import Organization, OrganizationMembership
from app.models.task import Task
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.schemas.task import TaskCreate


async def _run():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]

        owner = User(full_name="TOD Owner", email=f"tod.owner.{suffix}@example-corp.com", hashed_password="x", role="owner")
        tm = User(full_name="TOD TM", email=f"tod.tm.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MANAGER)
        pm = User(full_name="TOD PM", email=f"tod.pm.{suffix}@example-corp.com", hashed_password="x", role=PROJECT_MANAGER)
        bob = User(full_name="TOD Bob", email=f"tod.bob.{suffix}@example-corp.com", hashed_password="x", role="team_member")
        db.add_all([owner, tm, pm, bob])
        await db.commit()
        for u in (owner, tm, pm, bob):
            await db.refresh(u)

        org = Organization(name=f"TOD Org {suffix}", slug=f"tod-org-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.commit()
        org = (await db.execute(select(Organization).options(selectinload(Organization.subscription)).where(Organization.id == org.id))).scalar_one()

        memberships = {}
        for u, role in [(owner, OWNER), (tm, TEAM_MANAGER), (pm, PROJECT_MANAGER), (bob, "team_member")]:
            m = OrganizationMembership(organization_id=org.id, user_id=u.id, role=role)
            db.add(m)
            memberships[u.id] = m
        await db.commit()

        team_tech = Team(name=f"TOD Tech {suffix}", team_manager_id=tm.id, created_by_id=owner.id, organization_id=org.id)
        db.add(team_tech)
        await db.commit()
        await db.refresh(team_tech)
        db.add(TeamMembership(team_id=team_tech.id, user_id=tm.id))
        await db.commit()

        owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[owner.id], user=owner, db=db)
        tm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[tm.id], user=tm, db=db)
        pm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[pm.id], user=pm, db=db)

        created_task_ids: list[int] = []

        async def _create(payload, tenant):
            task = await create_task(payload, background_tasks=BackgroundTasks(), tenant=tenant, db=db)
            created_task_ids.append(task.id)
            return task

        try:
            # ── 1, 2. TM creates with no Team, no assignee -> self-assigned,
            # never an orphan. ────────────────────────────────────────────
            tm_personal = await _create(TaskCreate(name=f"TOD TM Personal {suffix}"), tm_tenant)
            assert tm_personal.assignee_id == tm.id, "a Team-less Task with no explicit assignee must default to the creator"
            assert tm_personal.team_id is None

            persisted = (await db.execute(select(Task).where(Task.id == tm_personal.id))).scalar_one()
            assert not (persisted.team_id is None and persisted.assignee_id is None), "must never persist as a fully-orphaned Task"

            # ── 4, 5. Appears in My Tasks AND in TM's All Tasks (personal-
            # owner union), created from the SAME route "All Tasks" would
            # use (there is only one create_task route). ──────────────────
            my_tasks = await list_my_tasks(
                status_filter=None, priority_filter=None, due_date_from=None, due_date_to=None,
                overdue=False, project_id=None, team_id=None, tenant=tm_tenant,
            )
            assert tm_personal.id in {t.id for t in my_tasks}

            tm_all_tasks = await list_tasks(
                status_filter=None, priority_filter=None, assignee_id=None, project_id=None, team_id=None,
                due_date_from=None, due_date_to=None, overdue=False, tenant=tm_tenant,
            )
            assert tm_personal.id in {t.id for t in tm_all_tasks}, "a TM's own Personal Task must appear in TM All Tasks via the personal-owner union"

            # ── 6. TM creates a Team Task with no assignee -> stays
            # Unassigned; visible in All Tasks, NOT in My Tasks. ────────────
            tm_team_task = await _create(TaskCreate(name=f"TOD TM Team Task {suffix}", team_id=team_tech.id), tm_tenant)
            assert tm_team_task.assignee_id is None, "a Team Task with no explicit assignee must stay Unassigned, never auto-assigned to the creator"
            assert tm_team_task.team_id == team_tech.id

            my_tasks_2 = await list_my_tasks(
                status_filter=None, priority_filter=None, due_date_from=None, due_date_to=None,
                overdue=False, project_id=None, team_id=None, tenant=tm_tenant,
            )
            assert tm_team_task.id not in {t.id for t in my_tasks_2}, "an Unassigned Team Task must not appear in My Tasks"

            tm_all_tasks_2 = await list_tasks(
                status_filter=None, priority_filter=None, assignee_id=None, project_id=None, team_id=None,
                due_date_from=None, due_date_to=None, overdue=False, tenant=tm_tenant,
            )
            assert tm_team_task.id in {t.id for t in tm_all_tasks_2}, "an Unassigned Team Task in a managed Team must still appear in All Tasks"

            # ── 13. PM personal standalone Task -> self-assigned. Project
            # is supplied here too (matching the spec's "PM creates:
            # Project=Zero Fund, Team=None, Assignee=None -> assignee_id=PM"
            # example); the fully project-less PM Personal Task case (no
            # Project, no Team) is now ALSO valid — see the dedicated
            # `test_pm_personal_task_and_edit_payload.py` follow-up, which
            # fixed `_PM_PROJECT_REQUIRED` to only apply when `team_id` is
            # set (Team delegation), not to a project-less Personal Task. ──
            from app.models.project import Project, ProjectMembership, ProjectTeam
            project = Project(name=f"TOD Project {suffix}", created_by_id=owner.id, organization_id=org.id)
            db.add(project)
            await db.commit()
            await db.refresh(project)
            db.add(ProjectMembership(project_id=project.id, user_id=pm.id))
            await db.commit()

            pm_personal = await _create(TaskCreate(name=f"TOD PM Personal {suffix}", project_id=project.id), pm_tenant)
            assert pm_personal.assignee_id == pm.id
            assert pm_personal.team_id is None

            pm_my_tasks = await list_my_tasks(
                status_filter=None, priority_filter=None, due_date_from=None, due_date_to=None,
                overdue=False, project_id=None, team_id=None, tenant=pm_tenant,
            )
            assert pm_personal.id in {t.id for t in pm_my_tasks}
            pm_all_tasks = await list_tasks(
                status_filter=None, priority_filter=None, assignee_id=None, project_id=None, team_id=None,
                due_date_from=None, due_date_to=None, overdue=False, tenant=pm_tenant,
            )
            assert pm_personal.id in {t.id for t in pm_all_tasks}, "a PM's own Personal Task must appear in PM All Tasks via the personal-owner union"

            # ── 16, 17. PM Project+Team delegation: assignee stays NULL,
            # PM never becomes assignee merely for creating it. ────────────
            db.add(TeamMembership(team_id=team_tech.id, user_id=pm.id))
            await db.commit()
            db.add(ProjectTeam(project_id=project.id, team_id=team_tech.id, assigned_by_id=owner.id))
            await db.commit()

            delegated = await _create(TaskCreate(name=f"TOD Delegated {suffix}", project_id=project.id, team_id=team_tech.id), pm_tenant)
            assert delegated.assignee_id is None, "a Project+Team delegated Task must stay Unassigned — the PM must never become its default assignee"
            assert delegated.team_id == team_tech.id
            assert delegated.project_id == project.id

            try:
                await _create(TaskCreate(name=f"TOD Delegated Bad {suffix}", project_id=project.id, team_id=team_tech.id, assignee_id=pm.id), pm_tenant)
                raise AssertionError("a plain PM must still never be able to name themselves (or anyone) on a delegated Team Task")
            except AppException as exc:
                assert exc.code == "PROJECT_MANAGER_CANNOT_ASSIGN_MEMBER", exc

            await db.execute(delete(ProjectTeam).where(ProjectTeam.project_id == project.id))
            await db.execute(delete(ProjectMembership).where(ProjectMembership.project_id == project.id))
            await db.execute(delete(Project).where(Project.id == project.id))
            await db.commit()

            # ── 33. Owner/Admin explicit-assignee creation is unaffected —
            # an Owner creating with no Team but an EXPLICIT other assignee
            # gets exactly that assignee, never overridden to self. ────────
            owner_explicit = await _create(TaskCreate(name=f"TOD Owner Explicit {suffix}", assignee_id=bob.id), owner_tenant)
            assert owner_explicit.assignee_id == bob.id, "an explicit assignee_id must never be silently overridden by the new default"

            # Owner/Admin explicit Team Task with no assignee still stays
            # Unassigned too (sanity — item 22).
            owner_team_task = await _create(TaskCreate(name=f"TOD Owner Team Task {suffix}", team_id=team_tech.id), owner_tenant)
            assert owner_team_task.assignee_id is None

        finally:
            if created_task_ids:
                await db.execute(delete(Task).where(Task.id.in_(created_task_ids)))
            await db.execute(delete(TeamMembership).where(TeamMembership.team_id == team_tech.id))
            await db.execute(delete(Team).where(Team.id == team_tech.id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id == org.id))
            await db.execute(delete(User).where(User.id.in_([owner.id, tm.id, pm.id, bob.id])))
            await db.commit()

    await engine.dispose()


def test_task_ownership_default_assignee():
    asyncio.run(_run())
