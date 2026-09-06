"""Regression tests for Task #5 — task description support
(app.models.task.Task.description / app.schemas.task.{TaskCreate,TaskUpdate,
TaskRead} / app.api.routes.tasks).

Covers:
  1. create task without description -> succeeds (backward compatible)
  2. create task with description -> persists
  3. task response returns description
  4. multi-line description is preserved verbatim
  5. update task description -> persists
  6. clear task description (empty string) -> normalizes to None
  7. existing (no-description) task continues to read/serialize fine
  8. a non-manager (team_member) cannot reach the update-task gate at all
     (require_org_manager, unchanged by this task — description rides the
     same authorization boundary as every other editable field)
  9. a user from a different organization cannot read/update the task
     (org-scoped repository lookup — get_task_or_404 returns 404, not the
     task, exactly as it already does for every other field)
  10. the task-request -> task conversion path preserves the request's
      existing description instead of silently dropping it (a real gap
      found during this task's investigation, not hypothetical)

Runs against the real database connection the app uses. Every row this
test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from fastapi import BackgroundTasks
from sqlalchemy import delete

from app.api.routes.task_requests import convert_task_request
from app.api.routes.tasks import create_task, get_task, update_task
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import TEAM_MEMBER
from app.core.tenant import TenantContext, require_org_manager
from app.models.organization import Organization, OrganizationMembership
from app.models.task import Task
from app.models.task_request import TaskRequest
from app.models.team import Team
from app.models.user import User
from app.schemas.task import TaskCreate, TaskUpdate
from app.schemas.task_request import TaskRequestConvert


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]

        owner = User(full_name="Desc Test Owner", email=f"desc.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        member = User(full_name="Desc Test Member", email=f"desc.member.{suffix}@test.invalid", hashed_password="x", role="team_member")
        outsider = User(full_name="Desc Test Outsider", email=f"desc.outsider.{suffix}@test.invalid", hashed_password="x", role="owner")
        db.add_all([owner, member, outsider])
        await db.commit()
        await db.refresh(owner)
        await db.refresh(member)
        await db.refresh(outsider)

        org = Organization(name=f"Desc Org {suffix}", slug=f"desc-org-{suffix}", owner_id=owner.id)
        other_org = Organization(name=f"Desc Other Org {suffix}", slug=f"desc-other-org-{suffix}", owner_id=outsider.id)
        db.add_all([org, other_org])
        await db.commit()
        await db.refresh(org)
        await db.refresh(other_org)

        owner_membership = OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner")
        member_membership = OrganizationMembership(organization_id=org.id, user_id=member.id, role=TEAM_MEMBER)
        outsider_membership = OrganizationMembership(organization_id=other_org.id, user_id=outsider.id, role="owner")
        db.add_all([owner_membership, member_membership, outsider_membership])
        await db.commit()
        await db.refresh(owner_membership)
        await db.refresh(member_membership)
        await db.refresh(outsider_membership)

        owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=owner_membership, user=owner, db=db)
        member_tenant = TenantContext(organization_id=org.id, organization=org, membership=member_membership, user=member, db=db)
        outsider_tenant = TenantContext(organization_id=other_org.id, organization=other_org, membership=outsider_membership, user=outsider, db=db)

        created_task_ids = []
        created_request_ids = []
        try:
            # ── 1. Create without description — backward compatible. ────────
            task_no_desc = await create_task(
                TaskCreate(name=f"No-description task {suffix}"),
                background_tasks=BackgroundTasks(), tenant=owner_tenant, db=db,
            )
            created_task_ids.append(task_no_desc.id)
            assert task_no_desc.description is None

            # ── 7. Re-fetching that task still works fine (existing task
            # without a description remains valid). ─────────────────────────
            refetched = await get_task(task_no_desc.id, tenant=owner_tenant)
            assert refetched.description is None
            assert refetched.name == task_no_desc.name

            # ── 2, 3, 4. Create with a multi-line description -> persists
            # and is returned verbatim, including line breaks. ──────────────
            multiline = "Line one.\nLine two with more detail.\n\nLine four after a blank line."
            task_with_desc = await create_task(
                TaskCreate(name=f"Described task {suffix}", description=multiline),
                background_tasks=BackgroundTasks(), tenant=owner_tenant, db=db,
            )
            created_task_ids.append(task_with_desc.id)
            assert task_with_desc.description == multiline

            refetched2 = await get_task(task_with_desc.id, tenant=owner_tenant)
            assert refetched2.description == multiline, "description must survive a fresh read, not just the create response"

            # ── 5. Update description -> persists. ───────────────────────────
            updated = await update_task(
                task_with_desc.id, TaskUpdate(description="Revised details after edit."),
                background_tasks=BackgroundTasks(), tenant=owner_tenant, db=db,
            )
            assert updated.description == "Revised details after edit."
            refetched3 = await get_task(task_with_desc.id, tenant=owner_tenant)
            assert refetched3.description == "Revised details after edit."

            # ── 6. Clear description (empty string) -> becomes None. ────────
            cleared = await update_task(
                task_with_desc.id, TaskUpdate(description=""),
                background_tasks=BackgroundTasks(), tenant=owner_tenant, db=db,
            )
            assert cleared.description is None
            refetched4 = await get_task(task_with_desc.id, tenant=owner_tenant)
            assert refetched4.description is None, "a cleared description must stay cleared on re-read, not just in the immediate response"

            # ── Updating unrelated fields must never disturb an existing
            # description (no accidental mass-assignment/overwrite). ────────
            task_keep_desc = await create_task(
                TaskCreate(name=f"Keep-description task {suffix}", description="Do not lose me."),
                background_tasks=BackgroundTasks(), tenant=owner_tenant, db=db,
            )
            created_task_ids.append(task_keep_desc.id)
            after_status_change = await update_task(
                task_keep_desc.id, TaskUpdate(status="in_progress"),
                background_tasks=BackgroundTasks(), tenant=owner_tenant, db=db,
            )
            assert after_status_change.description == "Do not lose me.", "updating an unrelated field must not clear the description"

            # ── 8. A plain team_member cannot even reach the update gate. ────
            try:
                await require_org_manager(tenant=member_tenant)
                raise AssertionError("a plain team_member must not pass require_org_manager")
            except AppException as exc:
                assert exc.status_code == 403, exc

            # ── 9. Cross-tenant isolation: an outsider (different org)
            # cannot read this task at all — org-scoped lookup returns 404. ──
            try:
                await get_task(task_with_desc.id, tenant=outsider_tenant)
                raise AssertionError("a user from a different organization must not be able to read this task")
            except AppException as exc:
                assert exc.status_code == 404, exc

            # ── 10. Task-request conversion preserves the request's
            # existing description instead of silently dropping it. ─────────
            from app.models.project import Project
            project = Project(name=f"Desc Project {suffix}", created_by_id=owner.id, organization_id=org.id)
            db.add(project)
            await db.commit()
            await db.refresh(project)

            team = Team(name=f"Desc Team {suffix}", team_manager_id=owner.id, created_by_id=owner.id, organization_id=org.id)
            db.add(team)
            await db.commit()
            await db.refresh(team)

            request = TaskRequest(
                organization_id=org.id, project_id=project.id, submitted_by_id=owner.id,
                title=f"Requested task {suffix}", description="Client-provided context that must not be lost.",
            )
            db.add(request)
            await db.commit()
            await db.refresh(request)
            created_request_ids.append(request.id)

            await convert_task_request(
                project_id=project.id, request_id=request.id,
                payload=TaskRequestConvert(team_id=team.id),
                background_tasks=BackgroundTasks(),
                tenant=owner_tenant,
            )
            # convert_task_request's own return shape isn't asserted here —
            # the real assertion is on the created Task row itself.
            from sqlalchemy import select
            result = await db.execute(select(Task).where(Task.name == f"Requested task {suffix}"))
            converted_task = result.scalar_one()
            created_task_ids.append(converted_task.id)
            assert converted_task.description == "Client-provided context that must not be lost.", (
                "converting a task request must preserve its existing description, not drop it"
            )

        finally:
            created_task_ids = [tid for tid in created_task_ids if tid]
            if created_task_ids:
                await db.execute(delete(Task).where(Task.id.in_(created_task_ids)))
            if created_request_ids:
                await db.execute(delete(TaskRequest).where(TaskRequest.id.in_(created_request_ids)))
            await db.execute(delete(Team).where(Team.organization_id == org.id))
            from app.models.project import Project as _Project
            await db.execute(delete(_Project).where(_Project.organization_id == org.id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_([org.id, other_org.id])))
            await db.execute(delete(Organization).where(Organization.id.in_([org.id, other_org.id])))
            await db.execute(delete(User).where(User.id.in_([owner.id, member.id, outsider.id])))
            await db.commit()

    await engine.dispose()


def test_task_description_full_lifecycle():
    asyncio.run(_scenario())
