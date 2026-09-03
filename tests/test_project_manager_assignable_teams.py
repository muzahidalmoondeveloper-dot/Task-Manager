"""Regression test for the Create Task "Assign Team" dropdown bug: a plain
Project Manager (not also Owner/Admin/Team Manager) opening a project they
manage saw no teams in the dropdown, because the frontend was populating it
from GET /teams — which is intentionally scoped to "teams you manage or are
a member of" and legitimately returns nothing for a PM who is neither.

Root cause: the dropdown was reading the wrong (deliberately narrower)
source. The fix reuses the *existing* project-scoped team list already
returned by GET /projects/{id}/items (derived from the project's own Rocks/
KPIs/Tasks — there's no explicit Project<->Team membership table in this
schema) instead of the global GET /teams list, for Project Managers only.

This test covers the corresponding write-side hardening in POST /tasks
(app.api.routes.tasks.create_task): even with the correct dropdown, the
server must still reject a team_id that isn't legitimately assignable
within the target project if a Project Manager submits one directly
(bypassing the UI), while never restricting Owner/Admin or introducing any
new restriction for a project_id-less request.

Covers:
  - GET /projects/{id}/items's `teams` field contains only the team
    actually associated with the project (via an existing Task there), not
    an unrelated team in the same org.
  - A Project Manager creating a task under their managed project with that
    associated team succeeds.
  - The same PM submitting an unrelated (but real, same-org) team_id is
    rejected with 403 — the write-side check, independent of the dropdown.
  - The same PM submitting a team_id belonging to a different organization
    is rejected (pre-existing "team must exist in this org" check, unaffected).
  - An Owner creating a task with that same unrelated team_id under the
    same project still succeeds — unchanged, unrestricted behavior.

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import delete

from app.api.routes.projects import get_project_items
from app.api.routes.tasks import create_task
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import PROJECT_MANAGER
from app.models.organization import Organization, OrganizationMembership
from app.models.project import Project, ProjectMembership
from app.models.task import Task
from app.models.team import Team
from app.models.user import User
from app.repositories.task_repository import TaskRepository
from app.schemas.task import TaskCreate


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="AssignScope Owner", email=f"assignscope.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        pm = User(full_name="AssignScope PM", email=f"assignscope.pm.{suffix}@test.invalid", hashed_password="x", role="project_manager")
        db.add_all([owner, pm])
        await db.flush()

        org = Organization(name=f"AssignScope Org {suffix}", slug=f"assignscope-{suffix}", owner_id=owner.id)
        other_org = Organization(name=f"AssignScope Other Org {suffix}", slug=f"assignscope-other-{suffix}", owner_id=owner.id)
        db.add_all([org, other_org])
        await db.flush()

        owner_membership = OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner")
        pm_membership = OrganizationMembership(organization_id=org.id, user_id=pm.id, role=PROJECT_MANAGER)
        db.add_all([owner_membership, pm_membership])
        await db.commit()
        await db.refresh(pm_membership)

        project = Project(name=f"Assignable Teams Project {suffix}", created_by_id=owner.id, organization_id=org.id)
        db.add(project)
        await db.flush()
        db.add(ProjectMembership(project_id=project.id, user_id=pm.id))

        team_associated = Team(name=f"Associated Team {suffix}", team_manager_id=owner.id, created_by_id=owner.id, organization_id=org.id)
        team_unrelated = Team(name=f"Unrelated Team {suffix}", team_manager_id=owner.id, created_by_id=owner.id, organization_id=org.id)
        team_cross_org = Team(name=f"Cross-Org Team {suffix}", team_manager_id=owner.id, created_by_id=owner.id, organization_id=other_org.id)
        db.add_all([team_associated, team_unrelated, team_cross_org])
        await db.commit()

        # Associate `team_associated` with the project the same way this
        # schema always does it — an existing Task under the project with
        # that team_id (no explicit Project<->Team table exists).
        task_repo = TaskRepository(db, org.id)
        seed_task = await task_repo.create(
            TaskCreate(name=f"Seed task {suffix}", project_id=project.id, team_id=team_associated.id),
            created_by_id=owner.id,
        )

        from app.core.tenant import TenantContext
        pm_tenant = TenantContext(organization_id=org.id, organization=org, membership=pm_membership, user=pm, db=db)
        owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=owner_membership, user=owner, db=db)

        created_task_ids = [seed_task.id]
        try:
            # ── GET /projects/{id}/items: only the associated team shows. ──
            items = await get_project_items(project.id, db=db, tenant=pm_tenant)
            item_team_ids = {t.id for t in items["teams"]}
            assert team_associated.id in item_team_ids
            assert team_unrelated.id not in item_team_ids, "an unrelated org team must not appear as assignable for this project"

            # ── PM creates a task with the project's own associated team: succeeds. ──
            created = await create_task(
                TaskCreate(name=f"PM valid team {suffix}", project_id=project.id, team_id=team_associated.id),
                background_tasks=BackgroundTasks(), tenant=pm_tenant, db=db,
            )
            created_task_ids.append(created.id)
            assert created.team_id == team_associated.id

            # ── PM submits a real, same-org, but unrelated team_id: rejected. ──
            try:
                await create_task(
                    TaskCreate(name=f"PM unrelated team {suffix}", project_id=project.id, team_id=team_unrelated.id),
                    background_tasks=BackgroundTasks(), tenant=pm_tenant, db=db,
                )
                raise AssertionError("a Project Manager must not be able to assign a team unrelated to the project")
            except HTTPException as exc:
                assert exc.status_code == 403, f"expected 403, got {exc.status_code}: {exc.detail}"

            # ── PM submits a cross-tenant team_id: rejected (pre-existing check). ──
            try:
                await create_task(
                    TaskCreate(name=f"PM cross-org team {suffix}", project_id=project.id, team_id=team_cross_org.id),
                    background_tasks=BackgroundTasks(), tenant=pm_tenant, db=db,
                )
                raise AssertionError("a team from another organization must never be assignable")
            except HTTPException as exc:
                assert exc.status_code == 400, f"expected 400, got {exc.status_code}: {exc.detail}"

            # ── Owner is unrestricted: the same "unrelated" team still works. ──
            owner_created = await create_task(
                TaskCreate(name=f"Owner unrelated team {suffix}", project_id=project.id, team_id=team_unrelated.id),
                background_tasks=BackgroundTasks(), tenant=owner_tenant, db=db,
            )
            created_task_ids.append(owner_created.id)
            assert owner_created.team_id == team_unrelated.id, "Owner/Admin team assignment must remain unrestricted"

        finally:
            await db.execute(delete(Task).where(Task.id.in_(created_task_ids)))
            await db.execute(delete(ProjectMembership).where(ProjectMembership.project_id == project.id))
            await db.execute(delete(Project).where(Project.id == project.id))
            await db.execute(delete(Team).where(Team.id.in_([team_associated.id, team_unrelated.id, team_cross_org.id])))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id.in_([org.id, other_org.id])))
            await db.execute(delete(User).where(User.id.in_([owner.id, pm.id])))
            await db.commit()

    await engine.dispose()


def test_project_manager_assignable_teams_scoped_to_project():
    asyncio.run(_scenario())
