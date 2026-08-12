"""Project Manager project-scope access control regression test.

REQUIREMENT: "Project Managers can only view and access projects that they
are the project manager of." app.core.tenant already restricted
`/projects` itself this way (`_is_project_scoped`/`_require_project_access`,
now promoted to the shared `app.core.project_access` module this test also
covers). Two real gaps existed in the task endpoints, which live under a
project but didn't consult that same rule:

- GET /tasks/project/{project_id} let a Project Manager NOT assigned to a
  project call it successfully (silently filtered to "your own assigned
  tasks" instead of a 403) — so a PM could confirm details about a project
  they don't manage instead of being refused outright. It also under-scoped
  the manager who WAS assigned: they only saw their own personally-assigned
  tasks instead of the whole project's tasks, which isn't "access to the
  project you manage" in any useful sense.
- GET /tasks/{task_id} blocked a Project Manager from viewing any task in
  a project they manage unless the task happened to be personally assigned
  to them — the inverse problem (under-permissioned for their own project).

This test drives the actual FastAPI route functions directly (constructing
a real TenantContext against real DB-loaded rows, the same object FastAPI's
dependency injection would build) — no HTTP client layer needed for a
correctness check like this.

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete

from app.api.routes.projects import get_project, list_projects
from app.api.routes.tasks import get_task, list_tasks_by_project
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import PROJECT_MANAGER
from app.core.tenant import TenantContext
from app.models.organization import Organization, OrganizationMembership
from app.models.project import Project, ProjectMembership
from app.models.task import Task
from app.models.user import User
from app.repositories.task_repository import TaskRepository
from app.schemas.task import TaskCreate


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="PMScope Owner", email=f"pmscope.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        pm = User(full_name="PMScope Manager", email=f"pmscope.pm.{suffix}@test.invalid", hashed_password="x", role="project_manager")
        other_assignee = User(full_name="PMScope Other", email=f"pmscope.other.{suffix}@test.invalid", hashed_password="x", role="team_member")
        db.add_all([owner, pm, other_assignee])
        await db.flush()

        org = Organization(name=f"PMScope Org {suffix}", slug=f"pmscope-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.flush()

        owner_membership = OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner")
        pm_membership = OrganizationMembership(organization_id=org.id, user_id=pm.id, role=PROJECT_MANAGER)
        other_membership = OrganizationMembership(organization_id=org.id, user_id=other_assignee.id, role="team_member")
        db.add_all([owner_membership, pm_membership, other_membership])
        await db.commit()
        await db.refresh(org)
        await db.refresh(pm_membership)

        project_a = Project(name=f"Project A {suffix}", created_by_id=owner.id, organization_id=org.id)
        project_b = Project(name=f"Project B {suffix}", created_by_id=owner.id, organization_id=org.id)
        db.add_all([project_a, project_b])
        await db.flush()
        # The PM is assigned to manage project A only.
        db.add(ProjectMembership(project_id=project_a.id, user_id=pm.id))
        await db.commit()

        task_repo = TaskRepository(db, org.id)
        task_a_other = await task_repo.create(
            TaskCreate(name=f"Task in A, not mine {suffix}", project_id=project_a.id, assignee_id=other_assignee.id),
            created_by_id=owner.id,
        )
        task_b = await task_repo.create(
            TaskCreate(name=f"Task in B {suffix}", project_id=project_b.id), created_by_id=owner.id,
        )

        pm_tenant = TenantContext(organization_id=org.id, organization=org, membership=pm_membership, user=pm, db=db)

        try:
            # ── GET /tasks/project/{A}: PM assigned here sees the WHOLE
            #    project's tasks, not only tasks assigned to themself. ──
            tasks_in_a = await list_tasks_by_project(project_a.id, tenant=pm_tenant)
            assert {t.id for t in tasks_in_a} == {task_a_other.id}, (
                "a Project Manager assigned to a project must see every task in it, not just their own"
            )

            # ── GET /tasks/project/{B}: PM NOT assigned here is refused outright. ──
            try:
                await list_tasks_by_project(project_b.id, tenant=pm_tenant)
                raise AssertionError("PROJECT_NOT_ASSIGNED must be raised for a project the PM does not manage")
            except AppException as exc:
                assert exc.code == "PROJECT_NOT_ASSIGNED", f"expected PROJECT_NOT_ASSIGNED, got {exc.code}"
                assert exc.status_code == 403

            # ── GET /tasks/{id}: PM may view a task in their managed
            #    project even though it's assigned to someone else. ──
            viewed = await get_task(task_a_other.id, tenant=pm_tenant)
            assert viewed.id == task_a_other.id

            # ── GET /tasks/{id}: PM may NOT view a task in an unmanaged project. ──
            try:
                await get_task(task_b.id, tenant=pm_tenant)
                raise AssertionError("viewing a task in an unmanaged project must be forbidden")
            except Exception as exc:
                from fastapi import HTTPException
                assert isinstance(exc, HTTPException) and exc.status_code == 403, (
                    f"expected a 403 HTTPException, got {type(exc).__name__}: {exc}"
                )

            # ── GET /projects: PM's list is scoped to project A only (the
            #    shared app.core.project_access rule this test module
            #    exercises, still enforced at the /projects routes too). ──
            visible_projects = await list_projects(tenant=pm_tenant)
            assert {p.id for p in visible_projects} == {project_a.id}, (
                "a Project Manager's project list must contain only projects they're assigned to"
            )

            # ── GET /projects/{B}: PM is refused outright. ──
            try:
                await get_project(project_b.id, tenant=pm_tenant)
                raise AssertionError("PROJECT_NOT_ASSIGNED must be raised for GET /projects/{id} on an unmanaged project")
            except AppException as exc:
                assert exc.code == "PROJECT_NOT_ASSIGNED"

        finally:
            await db.execute(delete(Task).where(Task.id.in_([task_a_other.id, task_b.id])))
            await db.execute(delete(ProjectMembership).where(ProjectMembership.project_id.in_([project_a.id, project_b.id])))
            await db.execute(delete(Project).where(Project.id.in_([project_a.id, project_b.id])))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id == org.id))
            await db.execute(delete(User).where(User.id.in_([owner.id, pm.id, other_assignee.id])))
            await db.commit()

    await engine.dispose()


def test_project_manager_scoped_to_only_their_assigned_projects():
    asyncio.run(_scenario())
