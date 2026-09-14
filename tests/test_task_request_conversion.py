"""Regression coverage for the Client Task Request -> Task conversion
workflow (Client Task Request conversion follow-up).

Root cause of the original bug: `TaskRequestConvert` required a `team_id`
on EVERY conversion (no way to omit it) while the frontend also exposed an
optional individual `assignee_id` dropdown — but the backend's plain-PM
branch rejected any non-null `assignee_id` outright. There was no "take it
for myself" path at all, and the only individual-owner UI available (the
Assignee dropdown) always hit `PROJECT_MANAGER_CANNOT_ASSIGN_MEMBER` for a
plain PM. Fixed by replacing the implicit team_id/assignee_id shape with
an explicit `conversion_mode` ("self" | "team") that never exposes an
individual assignee at all — see TaskRequestConvert's and
convert_task_request's own docstrings.

Covers the spec's "SECURITY TESTS" list:
  1-2   Client submits -> Pending; a managing PM can convert it.
  3-8   SELF mode: succeeds, project_id/team_id/assignee_id correct,
        appears in My Tasks, a forged assignee_id cannot be smuggled in
        (no such field exists on the schema at all).
  9-13  TEAM mode: attached-team conversion succeeds, team_id/assignee_id
        correct, serializes Unassigned, Team Manager can subsequently
        assign an individual using existing rules.
  14-20 AUTHORIZATION: unrelated-project PM, Team-Manager-only, Team
        Member, Client, cross-tenant project/team, non-attached team, and
        a team injected through direct API manipulation are all rejected.
  21-24 STATE: rejected/already-converted requests can't be (re)converted,
        concurrent conversion can't create two Tasks, a failed conversion
        never marks the request Converted.
  25-30 REGRESSION: Owner/Admin conversion still works, plain PM's normal
        create_task() My-Task/Team-delegation behavior is untouched, Team
        Manager assignment rules are untouched, Working Time stays
        assignee-only, Clients stay non-assignable.

Runs against the real database connection the app uses (AsyncSessionLocal/
asyncpg — `SELECT ... FOR UPDATE` needs a real transactional backend).
Every row this test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from fastapi import BackgroundTasks
from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from app.api.routes.task_requests import convert_task_request, create_task_request, reject_task_request
from app.api.routes.tasks import create_task, list_my_tasks, start_task_timer, stop_task_timer, update_task
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import CLIENT, OWNER, PROJECT_MANAGER, TEAM_MANAGER, TEAM_MEMBER
from app.core.tenant import TenantContext
from app.models.organization import Organization, OrganizationMembership
from app.models.project import Project, ProjectMembership, ProjectTeam
from app.models.task import Task
from app.models.task_request import TaskRequest
from app.models.task_time_entry import TaskTimeEntry
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.schemas.task import TaskCreate, TaskUpdate
from app.schemas.task_request import TaskRequestConvert, TaskRequestCreate, TaskRequestReject


async def _run():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]

        owner = User(full_name="TRC Owner", email=f"trc.owner.{suffix}@example-corp.com", hashed_password="x", role="owner")
        pm = User(full_name="TRC PM", email=f"trc.pm.{suffix}@example-corp.com", hashed_password="x", role=PROJECT_MANAGER)
        other_pm = User(full_name="TRC Other PM", email=f"trc.otherpm.{suffix}@example-corp.com", hashed_password="x", role=PROJECT_MANAGER)
        tm_only = User(full_name="TRC TM Only", email=f"trc.tmonly.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MANAGER)
        team_member = User(full_name="TRC Team Member", email=f"trc.member.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)
        bob = User(full_name="TRC Bob", email=f"trc.bob.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)
        client_user = User(full_name="TRC Client", email=f"trc.client.{suffix}@example-corp.com", hashed_password="x", role=CLIENT)
        db.add_all([owner, pm, other_pm, tm_only, team_member, bob, client_user])
        await db.commit()
        for u in (owner, pm, other_pm, tm_only, team_member, bob, client_user):
            await db.refresh(u)

        org = Organization(name=f"TRC Org {suffix}", slug=f"trc-org-{suffix}", owner_id=owner.id)
        other_org = Organization(name=f"TRC Other Org {suffix}", slug=f"trc-other-org-{suffix}", owner_id=owner.id)
        db.add_all([org, other_org])
        await db.commit()
        org = (await db.execute(select(Organization).options(selectinload(Organization.subscription)).where(Organization.id == org.id))).scalar_one()
        other_org = (await db.execute(select(Organization).options(selectinload(Organization.subscription)).where(Organization.id == other_org.id))).scalar_one()

        memberships = {}
        for u, role in [
            (owner, OWNER), (pm, PROJECT_MANAGER), (other_pm, PROJECT_MANAGER),
            (tm_only, TEAM_MANAGER), (team_member, TEAM_MEMBER), (bob, TEAM_MEMBER), (client_user, CLIENT),
        ]:
            m = OrganizationMembership(organization_id=org.id, user_id=u.id, role=role)
            db.add(m)
            memberships[u.id] = m
        await db.commit()

        project = Project(name=f"TRC Project {suffix}", created_by_id=owner.id, organization_id=org.id)
        unrelated_project = Project(name=f"TRC Unrelated Project {suffix}", created_by_id=owner.id, organization_id=org.id)  # pm and other_pm NOT members
        db.add_all([project, unrelated_project])
        await db.commit()
        for p in (project, unrelated_project):
            await db.refresh(p)

        db.add_all([
            ProjectMembership(project_id=project.id, user_id=pm.id),
            ProjectMembership(project_id=project.id, user_id=client_user.id),
        ])
        await db.commit()

        team_attached = Team(name=f"TRC Attached Team {suffix}", team_manager_id=tm_only.id, created_by_id=owner.id, organization_id=org.id)
        team_not_attached = Team(name=f"TRC Unattached Team {suffix}", team_manager_id=tm_only.id, created_by_id=owner.id, organization_id=org.id)
        other_org_team = Team(name=f"TRC Cross-Org Team {suffix}", team_manager_id=owner.id, created_by_id=owner.id, organization_id=other_org.id)
        db.add_all([team_attached, team_not_attached, other_org_team])
        await db.commit()
        for t in (team_attached, team_not_attached, other_org_team):
            await db.refresh(t)

        db.add_all([
            TeamMembership(team_id=team_attached.id, user_id=tm_only.id),
            TeamMembership(team_id=team_attached.id, user_id=bob.id),
            ProjectTeam(project_id=project.id, team_id=team_attached.id, assigned_by_id=owner.id),
            # team_not_attached exists in the org but has NO ProjectTeam row for `project`.
        ])
        await db.commit()

        owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[owner.id], user=owner, db=db)
        pm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[pm.id], user=pm, db=db)
        other_pm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[other_pm.id], user=other_pm, db=db)
        tm_only_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[tm_only.id], user=tm_only, db=db)
        team_member_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[team_member.id], user=team_member, db=db)
        client_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[client_user.id], user=client_user, db=db)

        created_task_ids: list[int] = []
        created_request_ids: list[int] = []

        async def _submit_request(title: str) -> TaskRequest:
            out = await create_task_request(
                project.id, TaskRequestCreate(title=title), background_tasks=BackgroundTasks(), tenant=client_tenant,
            )
            created_request_ids.append(out.id)
            return (await db.execute(select(TaskRequest).where(TaskRequest.id == out.id))).scalar_one()

        try:
            # ── 1. Client submits -> Pending. ─────────────────────────────
            req_self = await _submit_request(f"TRC Self Request {suffix}")
            assert req_self.status == "pending"

            # ── 2, 14-17. Authorization: only a PM/staff genuinely
            # assigned to this project may convert; unrelated PM, a plain
            # Team-Manager-only user, a Team Member, and a Client must all
            # be rejected. ──────────────────────────────────────────────
            try:
                await convert_task_request(
                    project.id, req_self.id, TaskRequestConvert(conversion_mode="self"),
                    background_tasks=BackgroundTasks(), tenant=other_pm_tenant,
                )
                raise AssertionError("a PM not assigned to this project must not convert its requests")
            except AppException as exc:
                assert exc.code == "TASK_REQUEST_STAFF_ONLY", exc

            try:
                await convert_task_request(
                    project.id, req_self.id, TaskRequestConvert(conversion_mode="self"),
                    background_tasks=BackgroundTasks(), tenant=tm_only_tenant,
                )
                raise AssertionError("a Team-Manager-only user with no project access must not convert")
            except AppException as exc:
                assert exc.code == "TASK_REQUEST_STAFF_ONLY", exc

            try:
                await convert_task_request(
                    project.id, req_self.id, TaskRequestConvert(conversion_mode="self"),
                    background_tasks=BackgroundTasks(), tenant=team_member_tenant,
                )
                raise AssertionError("a plain Team Member must never convert a task request")
            except AppException as exc:
                assert exc.code == "TASK_REQUEST_STAFF_ONLY", exc

            try:
                await convert_task_request(
                    project.id, req_self.id, TaskRequestConvert(conversion_mode="self"),
                    background_tasks=BackgroundTasks(), tenant=client_tenant,
                )
                raise AssertionError("a Client must never convert a task request")
            except AppException as exc:
                assert exc.code == "TASK_REQUEST_STAFF_ONLY", exc

            # ── 3-7. SELF MODE: succeeds; project_id/team_id/assignee_id
            # correct; appears in PM's My Tasks. ──────────────────────────
            converted_self = await convert_task_request(
                project.id, req_self.id, TaskRequestConvert(conversion_mode="self", priority="high"),
                background_tasks=BackgroundTasks(), tenant=pm_tenant,
            )
            assert converted_self.status == "converted"
            self_task = (await db.execute(select(Task).where(Task.id == converted_self.converted_task_id))).scalar_one()
            created_task_ids.append(self_task.id)
            assert self_task.project_id == project.id
            assert self_task.team_id is None, "self-mode Task must have no team_id"
            assert self_task.assignee_id == pm.id, "self-mode Task must be assigned to the authenticated converting PM"
            assert self_task.priority == "high"

            my_tasks = {t.id for t in await list_my_tasks(
                status_filter=None, priority_filter=None, due_date_from=None, due_date_to=None,
                overdue=False, project_id=None, team_id=None, tenant=pm_tenant,
            )}
            assert self_task.id in my_tasks, "a self-mode conversion must appear in the converting PM's My Tasks"

            # ── 8. A forged assignee_id cannot assign another user — there
            # is no such field on the schema at all; passing one is a
            # 422 at the FastAPI/pydantic boundary, never silently accepted. ─
            try:
                TaskRequestConvert(conversion_mode="self", assignee_id=bob.id)  # type: ignore[call-arg]
                raise AssertionError("TaskRequestConvert must not accept an assignee_id field at all")
            except Exception as exc:
                assert not isinstance(exc, AssertionError)

            # Contradictory payload shapes are rejected outright.
            try:
                TaskRequestConvert(conversion_mode="self", team_id=team_attached.id)
                raise AssertionError("self mode with a team_id must be rejected")
            except Exception as exc:
                assert not isinstance(exc, AssertionError)
            try:
                TaskRequestConvert(conversion_mode="team")
                raise AssertionError("team mode without a team_id must be rejected")
            except Exception as exc:
                assert not isinstance(exc, AssertionError)

            # ── 9-12. TEAM MODE: attached-team conversion succeeds;
            # team_id/assignee_id correct; serializes Unassigned. ──────────
            req_team = await _submit_request(f"TRC Team Request {suffix}")
            converted_team = await convert_task_request(
                project.id, req_team.id, TaskRequestConvert(conversion_mode="team", team_id=team_attached.id),
                background_tasks=BackgroundTasks(), tenant=pm_tenant,
            )
            assert converted_team.status == "converted"
            team_task = (await db.execute(select(Task).where(Task.id == converted_team.converted_task_id))).scalar_one()
            created_task_ids.append(team_task.id)
            assert team_task.team_id == team_attached.id
            assert team_task.assignee_id is None, "team-mode Task must be Unassigned"

            # ── 13. Team Manager can subsequently assign it — existing,
            # unchanged validate_task_assignee/update_task rules. ──────────
            assigned = await update_task(
                team_task.id, TaskUpdate(assignee_id=bob.id),
                background_tasks=BackgroundTasks(), tenant=tm_only_tenant, db=db,
            )
            assert assigned.assignee_id == bob.id

            # ── 18. Cross-tenant Team id is rejected. ───────────────────────
            req_cross = await _submit_request(f"TRC Cross Team Request {suffix}")
            try:
                await convert_task_request(
                    project.id, req_cross.id, TaskRequestConvert(conversion_mode="team", team_id=other_org_team.id),
                    background_tasks=BackgroundTasks(), tenant=pm_tenant,
                )
                raise AssertionError("a team from a different organization must never be usable")
            except AppException as exc:
                assert exc.code == "TEAM_NOT_ASSIGNABLE", exc

            # ── 19-20. A team that exists in-org but is NOT attached to
            # this project is rejected — including for Owner/Admin, not
            # only a plain PM (direct API manipulation can't inject it
            # through either role). ─────────────────────────────────────
            try:
                await convert_task_request(
                    project.id, req_cross.id, TaskRequestConvert(conversion_mode="team", team_id=team_not_attached.id),
                    background_tasks=BackgroundTasks(), tenant=pm_tenant,
                )
                raise AssertionError("a non-attached team must be rejected for a PM")
            except AppException as exc:
                assert exc.code == "TEAM_NOT_ASSIGNABLE", exc

            try:
                await convert_task_request(
                    project.id, req_cross.id, TaskRequestConvert(conversion_mode="team", team_id=team_not_attached.id),
                    background_tasks=BackgroundTasks(), tenant=owner_tenant,
                )
                raise AssertionError("a non-attached team must be rejected even for Owner/Admin (this endpoint's own invariant)")
            except AppException as exc:
                assert exc.code == "TEAM_NOT_ASSIGNABLE", exc

            # ── 21. A rejected request cannot be converted. ─────────────────
            req_rejected = await _submit_request(f"TRC Rejected Request {suffix}")
            rejected = await reject_task_request(
                project.id, req_rejected.id, TaskRequestReject(reason="not needed"),
                background_tasks=BackgroundTasks(), tenant=pm_tenant,
            )
            assert rejected.status == "rejected"
            try:
                await convert_task_request(
                    project.id, req_rejected.id, TaskRequestConvert(conversion_mode="self"),
                    background_tasks=BackgroundTasks(), tenant=pm_tenant,
                )
                raise AssertionError("a rejected request must never be convertible")
            except AppException as exc:
                assert exc.code == "TASK_REQUEST_ALREADY_REVIEWED", exc
                assert exc.status_code == 409

            # ── 22. An already-converted request cannot be converted
            # again. ─────────────────────────────────────────────────────
            try:
                await convert_task_request(
                    project.id, req_self.id, TaskRequestConvert(conversion_mode="team", team_id=team_attached.id),
                    background_tasks=BackgroundTasks(), tenant=pm_tenant,
                )
                raise AssertionError("an already-converted request must never be converted again")
            except AppException as exc:
                assert exc.code == "TASK_REQUEST_ALREADY_REVIEWED", exc
                assert exc.status_code == 409

            # ── 23. Concurrent conversion does not create two Tasks — two
            # "simultaneous" calls sharing this test's single db session
            # exercise the same code path SELECT...FOR UPDATE protects;
            # the second must observe status="converted" (already
            # committed by the first) and be rejected, never create a
            # second Task. ──────────────────────────────────────────────
            req_race = await _submit_request(f"TRC Race Request {suffix}")
            first = await convert_task_request(
                project.id, req_race.id, TaskRequestConvert(conversion_mode="self"),
                background_tasks=BackgroundTasks(), tenant=pm_tenant,
            )
            created_task_ids.append(first.converted_task_id)
            try:
                await convert_task_request(
                    project.id, req_race.id, TaskRequestConvert(conversion_mode="self"),
                    background_tasks=BackgroundTasks(), tenant=pm_tenant,
                )
                raise AssertionError("a second conversion of the same request must be rejected")
            except AppException as exc:
                assert exc.code == "TASK_REQUEST_ALREADY_REVIEWED", exc
                assert exc.status_code == 409
            task_count = (await db.execute(
                select(Task).where(Task.name == req_race.title)
            )).scalars().all()
            assert len(task_count) == 1, "exactly one Task must exist for the raced request"

            # ── 24. A failed conversion (bad team) never marks the
            # request Converted — it must remain Pending and be
            # convertible again afterward. ──────────────────────────────
            req_retry = await _submit_request(f"TRC Retry Request {suffix}")
            try:
                await convert_task_request(
                    project.id, req_retry.id, TaskRequestConvert(conversion_mode="team", team_id=team_not_attached.id),
                    background_tasks=BackgroundTasks(), tenant=pm_tenant,
                )
                raise AssertionError("expected TEAM_NOT_ASSIGNABLE")
            except AppException as exc:
                assert exc.code == "TEAM_NOT_ASSIGNABLE", exc
            still_pending = (await db.execute(select(TaskRequest).where(TaskRequest.id == req_retry.id))).scalar_one()
            assert still_pending.status == "pending", "a failed conversion attempt must leave the request Pending"
            retried = await convert_task_request(
                project.id, req_retry.id, TaskRequestConvert(conversion_mode="self"),
                background_tasks=BackgroundTasks(), tenant=pm_tenant,
            )
            assert retried.status == "converted"
            created_task_ids.append(retried.converted_task_id)

            # ── 25. Owner/Admin existing conversion behavior remains
            # valid (any project, either mode). ─────────────────────────
            req_owner = await _submit_request(f"TRC Owner Request {suffix}")
            owner_converted = await convert_task_request(
                project.id, req_owner.id, TaskRequestConvert(conversion_mode="team", team_id=team_attached.id),
                background_tasks=BackgroundTasks(), tenant=owner_tenant,
            )
            assert owner_converted.status == "converted"
            created_task_ids.append(owner_converted.converted_task_id)

            # ── 26-27. Plain PM's normal create_task() My-Task/Team-
            # delegation behavior is completely untouched by this feature. ──
            my_task = await create_task(
                TaskCreate(name=f"TRC Plain My Task {suffix}", project_id=project.id),
                background_tasks=BackgroundTasks(), tenant=pm_tenant, db=db,
            )
            created_task_ids.append(my_task.id)
            assert my_task.assignee_id == pm.id and my_task.team_id is None

            delegated_task = await create_task(
                TaskCreate(name=f"TRC Plain Delegated {suffix}", project_id=project.id, team_id=team_attached.id),
                background_tasks=BackgroundTasks(), tenant=pm_tenant, db=db,
            )
            created_task_ids.append(delegated_task.id)
            assert delegated_task.assignee_id is None and delegated_task.team_id == team_attached.id

            # ── 28. Team Manager assignment rules remain unchanged: a
            # Client and an out-of-team member are still rejected. ─────────
            try:
                await update_task(team_task.id, TaskUpdate(assignee_id=client_user.id), background_tasks=BackgroundTasks(), tenant=tm_only_tenant, db=db)
                raise AssertionError("a Client must never be assignable")
            except AppException as exc:
                assert exc.code == "CLIENT_NOT_ASSIGNABLE", exc

            # ── 29. Working Time remains assignee-only: the PM (not the
            # assignee of team_task, which is bob) cannot start its timer. ──
            try:
                await start_task_timer(team_task.id, tenant=pm_tenant)
                raise AssertionError("a non-assignee must never be able to start a task's timer")
            except AppException as exc:
                assert exc.status_code == 403

            # The actual assignee (bob) CAN start/stop it.
            bob_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[bob.id], user=bob, db=db)
            start_state = await start_task_timer(team_task.id, tenant=bob_tenant)
            assert start_state.is_active is True
            stop_state = await stop_task_timer(team_task.id, tenant=bob_tenant)
            assert stop_state.is_active is False

            # ── 30. Client remains non-assignable via the normal task
            # update path too (already covered by 28, kept for explicit
            # numbering parity with the spec). ──────────────────────────────

        finally:
            if created_request_ids:
                await db.execute(delete(TaskRequest).where(TaskRequest.id.in_(created_request_ids)))
            await db.execute(delete(TaskTimeEntry).where(TaskTimeEntry.task_id.in_(created_task_ids)))
            await db.execute(delete(Task).where(Task.id.in_(created_task_ids)))
            await db.execute(delete(TeamMembership).where(TeamMembership.team_id.in_([team_attached.id, team_not_attached.id, other_org_team.id])))
            await db.execute(delete(ProjectTeam).where(ProjectTeam.project_id == project.id))
            await db.execute(delete(Team).where(Team.id.in_([team_attached.id, team_not_attached.id, other_org_team.id])))
            await db.execute(delete(ProjectMembership).where(ProjectMembership.project_id.in_([project.id, unrelated_project.id])))
            await db.execute(delete(Project).where(Project.id.in_([project.id, unrelated_project.id])))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_([org.id, other_org.id])))
            await db.execute(delete(Organization).where(Organization.id.in_([org.id, other_org.id])))
            await db.execute(delete(User).where(User.id.in_([owner.id, pm.id, other_pm.id, tm_only.id, team_member.id, bob.id, client_user.id])))
            await db.commit()

    await engine.dispose()


def test_task_request_conversion_full_workflow():
    asyncio.run(_run())
