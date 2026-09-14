"""Regression tests for the Team To-Do Project-field follow-up (Issue 2).

ARCHITECTURE AUDIT (required before implementing): Team Detail -> To-Do
uses the EXISTING Task model/API end-to-end — `CreateTodoModal` in
TeamDetailPage.jsx calls `taskApi.create`/`taskApi.update`, i.e.
`POST /tasks` / `PATCH /tasks/{id}` (app.api.routes.tasks), the identical
routes the classic Task modal uses, always with `team_id` fixed to the
current Team. There is no separate To-Do model, schema, or route. `Task.
project_id` already existed and was already fully wired through
create/update/serialization (`serialize_task` already includes it) — the
ONLY gap was that `CreateTodoModal`'s own form/payload never included a
Project field or `project_id` at all. No schema/migration change was
needed; the fix is a Task-model consumer wiring gap, not a missing
capability.

Since Team To-Do IS a Task with `team_id` always set, its Project
independence/validation is the exact same code path already exhaustively
covered by test_tm_task_project_team_independence.py (no ProjectTeam
attachment required, cross-tenant/nonexistent project_id rejected,
Project selection never alters Assignee eligibility). This file adds the
narrower, Team-To-Do-shaped scenarios the spec calls out explicitly:
project=NULL create, project=Clarvs create with no attachment, edit
add/change/remove Project, and serialization round-trip.

Covers spec test items 18-25.

Runs against the real database connection the app uses. Every row this
test creates is deleted before it returns.
"""

import asyncio
import uuid

from fastapi import BackgroundTasks
from sqlalchemy import select
from sqlalchemy.orm import selectinload

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from fastapi import HTTPException

from sqlalchemy import delete

from app.api.routes.tasks import create_task, update_task
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import OWNER, TEAM_MANAGER, TEAM_MEMBER
from app.core.tenant import TenantContext
from app.models.organization import Organization, OrganizationMembership
from app.models.project import Project
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.schemas.task import TaskCreate, TaskUpdate


async def _run():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]

        owner = User(full_name="TTP Owner", email=f"ttp.owner.{suffix}@example-corp.com", hashed_password="x", role="owner")
        tm = User(full_name="TTP TM", email=f"ttp.tm.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MANAGER)
        member = User(full_name="TTP Member", email=f"ttp.member.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)
        db.add_all([owner, tm, member])
        await db.commit()
        for u in (owner, tm, member):
            await db.refresh(u)

        org = Organization(name=f"TTP Org {suffix}", slug=f"ttp-org-{suffix}", owner_id=owner.id)
        other_org = Organization(name=f"TTP Other Org {suffix}", slug=f"ttp-other-org-{suffix}", owner_id=owner.id)
        db.add_all([org, other_org])
        await db.commit()
        org = (await db.execute(select(Organization).options(selectinload(Organization.subscription)).where(Organization.id == org.id))).scalar_one()

        memberships = {}
        for u, role in [(owner, OWNER), (tm, TEAM_MANAGER), (member, TEAM_MEMBER)]:
            m = OrganizationMembership(organization_id=org.id, user_id=u.id, role=role)
            db.add(m)
            memberships[u.id] = m
        await db.commit()

        technology = Team(name=f"TTP Technology {suffix}", team_manager_id=tm.id, created_by_id=owner.id, organization_id=org.id)
        db.add(technology)
        await db.commit()
        await db.refresh(technology)
        db.add_all([
            TeamMembership(team_id=technology.id, user_id=tm.id),
            TeamMembership(team_id=technology.id, user_id=member.id),
        ])
        await db.commit()

        # Deliberately NO ProjectTeam attachment to `technology` anywhere.
        clarvs = Project(name=f"Clarvs {suffix}", created_by_id=owner.id, organization_id=org.id, status="active")
        mailhub = Project(name=f"MailHub {suffix}", created_by_id=owner.id, organization_id=org.id, status="active")
        db.add_all([clarvs, mailhub])
        await db.commit()
        for p in (clarvs, mailhub):
            await db.refresh(p)

        cross_tenant_project = Project(name=f"TTP CrossTenant {suffix}", created_by_id=owner.id, organization_id=other_org.id, status="active")
        db.add(cross_tenant_project)
        await db.commit()
        await db.refresh(cross_tenant_project)

        tm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[tm.id], user=tm, db=db)

        created_task_ids: list[int] = []

        async def _create_todo(payload, tenant=tm_tenant):
            todo = await create_task(payload, background_tasks=BackgroundTasks(), tenant=tenant, db=db)
            created_task_ids.append(todo.id)
            return todo

        async def _update_todo(todo_id, payload, tenant=tm_tenant):
            return await update_task(todo_id, payload, background_tasks=BackgroundTasks(), tenant=tenant, db=db)

        try:
            # ── 18. project=NULL -> succeeds. ────────────────────────────
            todo_no_project = await _create_todo(
                TaskCreate(name=f"TTP To-Do No Project {suffix}", team_id=technology.id, assignee_id=member.id)
            )
            assert todo_no_project.project_id is None
            assert todo_no_project.team_id == technology.id

            # ── 19, 20. project=Clarvs -> succeeds, no attachment needed. ──
            todo_with_project = await _create_todo(
                TaskCreate(name=f"TTP To-Do Clarvs {suffix}", team_id=technology.id, project_id=clarvs.id, assignee_id=member.id)
            )
            assert todo_with_project.project_id == clarvs.id
            assert todo_with_project.team_id == technology.id

            # ── 21. project_id persists and serializes (re-fetch via
            # update_task's own returned/serialized object, mirroring what
            # GET would also return). ────────────────────────────────────
            reread = await _update_todo(todo_with_project.id, TaskUpdate(priority="high"))
            assert reread.project_id == clarvs.id, "project_id must persist and round-trip through serialization"
            assert reread.priority == "high"

            # ── 22. Edit To-Do can add/change/remove Project. ───────────
            added = await _update_todo(todo_no_project.id, TaskUpdate(project_id=mailhub.id))
            assert added.project_id == mailhub.id
            changed = await _update_todo(todo_no_project.id, TaskUpdate(project_id=clarvs.id))
            assert changed.project_id == clarvs.id
            removed = await _update_todo(todo_no_project.id, TaskUpdate(project_id=None))
            assert removed.project_id is None

            # ── 23. cross-tenant/nonexistent Project -> rejected, both on
            # create and edit. ───────────────────────────────────────────
            try:
                await _create_todo(TaskCreate(name=f"TTP Bad {suffix}", team_id=technology.id, project_id=cross_tenant_project.id))
                raise AssertionError("a cross-tenant project_id must be rejected on To-Do create")
            except HTTPException as exc:
                assert exc.status_code == 400, exc
            try:
                await _create_todo(TaskCreate(name=f"TTP Bad {suffix}", team_id=technology.id, project_id=999_999_999))
                raise AssertionError("a nonexistent project_id must be rejected on To-Do create")
            except HTTPException as exc:
                assert exc.status_code == 400, exc
            try:
                await _update_todo(todo_no_project.id, TaskUpdate(project_id=cross_tenant_project.id))
                raise AssertionError("a cross-tenant project_id must be rejected on To-Do edit")
            except HTTPException as exc:
                assert exc.status_code == 400, exc

            # ── 24. Project selection does not alter Assignee options —
            # the same eligible Team member remains assignable regardless
            # of which Project (if any) is selected. ────────────────────
            reassigned = await _update_todo(todo_with_project.id, TaskUpdate(assignee_id=tm.id))
            assert reassigned.assignee_id == tm.id
            assert reassigned.project_id == clarvs.id, "reassigning must not disturb the existing Project link"

            # ── 25. Team scope remains fixed — team_id is never implicitly
            # changed by any of the above Project-only edits. ────────────
            assert reassigned.team_id == technology.id

        finally:
            if created_task_ids:
                from app.models.task import Task
                await db.execute(delete(Task).where(Task.id.in_(created_task_ids)))
            await db.execute(delete(Project).where(Project.id.in_([clarvs.id, mailhub.id, cross_tenant_project.id])))
            await db.execute(delete(TeamMembership).where(TeamMembership.team_id == technology.id))
            await db.execute(delete(Team).where(Team.id == technology.id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id.in_([org.id, other_org.id])))
            await db.execute(delete(User).where(User.id.in_([owner.id, tm.id, member.id])))
            await db.commit()

    await engine.dispose()


def test_team_todo_project_field():
    asyncio.run(_run())
