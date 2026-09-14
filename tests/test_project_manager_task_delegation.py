"""Regression tests for the Project Manager Task-delegation follow-up
(app.api.routes.tasks.create_task's new `is_plain_project_manager`
assignee invariant, and the matching fix in
app.api.routes.task_requests.convert_task_request).

Business rule: a plain Project Manager (has_project_manager_access, no
Owner/Admin/Team-Manager capability) creating a Task through the Project-
management flow may never directly name an individual assignee.

  - team_id set (a "Project Task"): the Task is delegated to that Team —
    assignee_id MUST be NULL, even the PM's own id. The Team Manager
    decides the individual owner afterward.
  - team_id absent (a "My Task"): personal PM work with no Team involved
    at all — assignee_id is fixed to the PM themself; an explicit
    different value is rejected, never silently corrected.

Owners/Admins/Team Managers are completely unaffected — this only ever
applies to a plain Project Manager's own manual creation through this
route (never automation/meeting-extraction, which never reaches this
human-actor permission gate at all).

Covers (see the spec's "TESTS" sections):
  1-9   My Task creation: succeeds, assignee forced to self, team_id
        NULL, Project required, unrelated Project rejected, manipulated
        assignee rejected, appears in My Tasks, PM can control the timer
        because they are the real assignee.
  10-17 Project Task creation: succeeds, Project required, Team must be
        attached to the Project, unrelated/cross-tenant Team rejected,
        assignee_id persists NULL, manipulated assignee_id (including the
        PM's own id) is rejected.
  18-19 Delegated Task visibility: appears in PM All Tasks AND in the
        managing Team Manager's Team-scoped All Tasks.
  20-23 Team Manager assignment: may assign themselves, may assign an
        eligible Team member, Client and wrong-Team member remain
        rejected (existing, unchanged validate_task_assignee rules).
  24-26 PM cannot reassign via PATCH /tasks/{id} either: a later follow-up
        (Project Manager Task-management) gives a plain PM a SCOPED
        update path on their own managed-project tasks, but assignee_id
        stays completely off-limits through that same route — rejecting
        Team Member, Team Manager, and even the PM's own id.
  27-29 Owner/Admin/Team Manager existing update-assignment behavior is
        unaffected; a plain PM CAN update an unrelated allowed field
        (e.g. priority) without disturbing the existing assignee.

Also covers the same fix in the Task Request -> Task conversion flow
(POST /projects/{id}/task-requests/{id}/convert), a second, independently
-discovered surface that built its own TaskCreate directly and would
otherwise have bypassed create_task()'s new restriction entirely.

Runs against the real database connection the app uses. Every row this
test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from fastapi import BackgroundTasks
from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from app.api.routes.task_requests import convert_task_request
from app.api.routes.tasks import create_task, list_tasks, start_task_timer, stop_task_timer, update_task
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import CLIENT, OWNER, PROJECT_MANAGER, TEAM_MANAGER, TEAM_MEMBER
from app.core.tenant import TenantContext
from app.models.organization import Organization, OrganizationMembership
from app.models.project import Project, ProjectMembership, ProjectTeam
from app.models.task import Task
from app.models.task_time_entry import TaskTimeEntry
from app.models.task_request import TaskRequest
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.schemas.task import TaskCreate, TaskUpdate
from app.schemas.task_request import TaskRequestConvert


async def _run():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]

        owner = User(full_name="PMTD Owner", email=f"pmtd.owner.{suffix}@example-corp.com", hashed_password="x", role="owner")
        pm = User(full_name="PMTD PM", email=f"pmtd.pm.{suffix}@example-corp.com", hashed_password="x", role=PROJECT_MANAGER)
        tm = User(full_name="PMTD TM", email=f"pmtd.tm.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MANAGER)
        bob = User(full_name="PMTD Bob", email=f"pmtd.bob.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)
        alice = User(full_name="PMTD Alice", email=f"pmtd.alice.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)
        client_user = User(full_name="PMTD Client", email=f"pmtd.client.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)
        db.add_all([owner, pm, tm, bob, alice, client_user])
        await db.commit()
        for u in (owner, pm, tm, bob, alice, client_user):
            await db.refresh(u)

        org = Organization(name=f"PMTD Org {suffix}", slug=f"pmtd-org-{suffix}", owner_id=owner.id)
        other_org = Organization(name=f"PMTD Other Org {suffix}", slug=f"pmtd-other-org-{suffix}", owner_id=owner.id)
        db.add_all([org, other_org])
        await db.commit()
        org = (await db.execute(select(Organization).options(selectinload(Organization.subscription)).where(Organization.id == org.id))).scalar_one()
        other_org = (await db.execute(select(Organization).options(selectinload(Organization.subscription)).where(Organization.id == other_org.id))).scalar_one()

        memberships = {}
        for u, role in [(owner, OWNER), (pm, PROJECT_MANAGER), (tm, TEAM_MANAGER), (bob, TEAM_MEMBER), (alice, TEAM_MEMBER), (client_user, CLIENT)]:
            m = OrganizationMembership(organization_id=org.id, user_id=u.id, role=role)
            db.add(m)
            memberships[u.id] = m
        await db.commit()

        project_a = Project(name=f"PMTD Project A {suffix}", created_by_id=owner.id, organization_id=org.id)
        project_b = Project(name=f"PMTD Project B {suffix}", created_by_id=owner.id, organization_id=org.id)  # pm NOT a member
        db.add_all([project_a, project_b])
        await db.commit()
        for p in (project_a, project_b):
            await db.refresh(p)
        db.add(ProjectMembership(project_id=project_a.id, user_id=pm.id))
        await db.commit()

        team_a = Team(name=f"PMTD Team A {suffix}", team_manager_id=tm.id, created_by_id=owner.id, organization_id=org.id)
        team_c = Team(name=f"PMTD Team C {suffix}", team_manager_id=tm.id, created_by_id=owner.id, organization_id=org.id)  # exists, NOT attached to project_a
        other_org_team = Team(name=f"PMTD Other Org Team {suffix}", team_manager_id=owner.id, created_by_id=owner.id, organization_id=other_org.id)
        db.add_all([team_a, team_c, other_org_team])
        await db.commit()
        for t in (team_a, team_c, other_org_team):
            await db.refresh(t)
        db.add_all([
            TeamMembership(team_id=team_a.id, user_id=tm.id),
            TeamMembership(team_id=team_a.id, user_id=bob.id),
            # alice is deliberately NOT on team_a — the "wrong-team member" probe.
            ProjectTeam(project_id=project_a.id, team_id=team_a.id, assigned_by_id=owner.id),
            # team_c exists in the org but is NOT attached to project_a.
        ])
        await db.commit()

        owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[owner.id], user=owner, db=db)
        pm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[pm.id], user=pm, db=db)
        tm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[tm.id], user=tm, db=db)

        created_task_ids: list[int] = []
        created_request_ids: list = []

        async def _create(payload, tenant):
            return await create_task(payload, background_tasks=BackgroundTasks(), tenant=tenant, db=db)

        try:
            # ── 1, 2, 3, 7. My Task: succeeds, assignee forced to self,
            # team_id NULL, project required. ────────────────────────────────
            my_task = await _create(
                TaskCreate(name=f"PMTD My Task {suffix}", project_id=project_a.id),
                pm_tenant,
            )
            created_task_ids.append(my_task.id)
            assert my_task.assignee_id == pm.id, "a plain PM's team-less Task must be self-assigned"
            assert my_task.team_id is None

            my_tasks_seen = {t.id for t in (await list_tasks(
                status_filter=None, priority_filter=None, assignee_id=pm.id, project_id=None, team_id=None,
                due_date_from=None, due_date_to=None, overdue=False, tenant=pm_tenant,
            ))}
            assert my_task.id in my_tasks_seen, "the newly-created My Task must appear when filtered to the PM's own assignee_id"

            # ── 4. Project required. ──────────────────────────────────────────
            try:
                await _create(TaskCreate(name=f"PMTD No Project {suffix}"), pm_tenant)
                raise AssertionError("a plain PM must not be able to create a project-less Task")
            except AppException as exc:
                assert exc.code == "PROJECT_REQUIRED", exc

            # ── 5. Unrelated project rejected. ────────────────────────────────
            try:
                await _create(TaskCreate(name=f"PMTD Unrelated {suffix}", project_id=project_b.id), pm_tenant)
                raise AssertionError("a plain PM must not create a Task under a project they don't manage")
            except Exception as exc:
                assert getattr(exc, "status_code", None) == 403, exc

            # ── 6. Manipulated assignee on a My Task (team_id absent) is
            # rejected, never silently corrected. ─────────────────────────────
            try:
                await _create(TaskCreate(name=f"PMTD Bad Self {suffix}", project_id=project_a.id, assignee_id=bob.id), pm_tenant)
                raise AssertionError("a plain PM must not be able to name another individual even on a team-less Task")
            except AppException as exc:
                assert exc.code == "PROJECT_MANAGER_CANNOT_ASSIGN_MEMBER", exc

            # ── 8. PM can control the timer on My Task — they ARE the
            # real assignee. ──────────────────────────────────────────────────
            start_state = await start_task_timer(my_task.id, tenant=pm_tenant)
            assert start_state.is_active is True
            stop_state = await stop_task_timer(my_task.id, tenant=pm_tenant)
            assert stop_state.is_active is False

            # ── 10, 16. Project Task: succeeds, assignee_id persists NULL. ────
            project_task = await _create(
                TaskCreate(name=f"PMTD Project Task {suffix}", project_id=project_a.id, team_id=team_a.id),
                pm_tenant,
            )
            created_task_ids.append(project_task.id)
            assert project_task.assignee_id is None, "a PM-delegated Project Task must be created Unassigned"
            assert project_task.team_id == team_a.id

            # ── 14. A team that exists in the org but is NOT attached to
            # this project is rejected. ────────────────────────────────────────
            try:
                await _create(TaskCreate(name=f"PMTD Wrong Team {suffix}", project_id=project_a.id, team_id=team_c.id), pm_tenant)
                raise AssertionError("a team not attached to this project must be rejected")
            except Exception as exc:
                assert getattr(exc, "status_code", None) == 403, exc

            # ── 15. Cross-tenant team rejected. ───────────────────────────────
            try:
                await _create(TaskCreate(name=f"PMTD Cross Org Team {suffix}", project_id=project_a.id, team_id=other_org_team.id), pm_tenant)
                raise AssertionError("a team from a different organization must never be usable")
            except Exception as exc:
                assert getattr(exc, "status_code", None) in (400, 403), exc

            # ── 17. Manipulated assignee on a Project Task is rejected — even
            # the PM's own id (self-assignment on a delegated Team Task
            # bypasses the Team Manager's ownership decision, so it's
            # rejected exactly like assigning anyone else). ───────────────────
            try:
                await _create(TaskCreate(name=f"PMTD Bad Delegate {suffix}", project_id=project_a.id, team_id=team_a.id, assignee_id=bob.id), pm_tenant)
                raise AssertionError("a plain PM must not directly assign a Team Member to a delegated Team Task")
            except AppException as exc:
                assert exc.code == "PROJECT_MANAGER_CANNOT_ASSIGN_MEMBER", exc

            try:
                await _create(TaskCreate(name=f"PMTD Bad Self Delegate {suffix}", project_id=project_a.id, team_id=team_a.id, assignee_id=pm.id), pm_tenant)
                raise AssertionError("a plain PM must not assign a delegated Team Task to themselves either")
            except AppException as exc:
                assert exc.code == "PROJECT_MANAGER_CANNOT_ASSIGN_MEMBER", exc

            # ── 18, 19. Delegated Task appears in PM All Tasks AND in the
            # managing Team Manager's Team-scoped All Tasks. ──────────────────
            pm_all_ids = {t.id for t in await list_tasks(
                status_filter=None, priority_filter=None, assignee_id=None, project_id=None, team_id=None,
                due_date_from=None, due_date_to=None, overdue=False, tenant=pm_tenant,
            )}
            assert project_task.id in pm_all_ids

            tm_all_ids = {t.id for t in await list_tasks(
                status_filter=None, priority_filter=None, assignee_id=None, project_id=None, team_id=None,
                due_date_from=None, due_date_to=None, overdue=False, tenant=tm_tenant,
            )}
            assert project_task.id in tm_all_ids, "a delegated Task must enter the managing Team Manager's Team scope"

            # ── 20, 21. Team Manager may assign themselves or an eligible
            # Team member. ─────────────────────────────────────────────────
            assigned_to_tm = await update_task(
                project_task.id, TaskUpdate(assignee_id=tm.id),
                background_tasks=BackgroundTasks(), tenant=tm_tenant, db=db,
            )
            assert assigned_to_tm.assignee_id == tm.id

            assigned_to_bob = await update_task(
                project_task.id, TaskUpdate(assignee_id=bob.id),
                background_tasks=BackgroundTasks(), tenant=tm_tenant, db=db,
            )
            assert assigned_to_bob.assignee_id == bob.id

            # ── 22, 23. Client and wrong-Team member remain rejected —
            # existing, unchanged validate_task_assignee rules. ──────────────
            try:
                await update_task(project_task.id, TaskUpdate(assignee_id=client_user.id), background_tasks=BackgroundTasks(), tenant=tm_tenant, db=db)
                raise AssertionError("a Client must never be assignable")
            except AppException as exc:
                assert exc.code == "CLIENT_NOT_ASSIGNABLE", exc

            try:
                await update_task(project_task.id, TaskUpdate(assignee_id=alice.id), background_tasks=BackgroundTasks(), tenant=tm_tenant, db=db)
                raise AssertionError("a user outside this exact Team must never be assignable to its Task")
            except AppException as exc:
                assert exc.code == "ASSIGNEE_NOT_TEAM_MEMBER", exc

            # ── 24, 25, 26. PM cannot reassign via PATCH either — even
            # though the Project Manager Task-management follow-up (a
            # later task) gives a plain PM a SCOPED update path on their
            # own managed-project tasks for core fields, assignee_id
            # remains completely off-limits through that same route. ────────
            try:
                await update_task(project_task.id, TaskUpdate(assignee_id=bob.id), background_tasks=BackgroundTasks(), tenant=pm_tenant, db=db)
                raise AssertionError("a plain Project Manager must not be able to PATCH assignee_id to a Team Member")
            except AppException as exc:
                assert exc.code == "PROJECT_MANAGER_CANNOT_ASSIGN_MEMBER", exc

            try:
                await update_task(project_task.id, TaskUpdate(assignee_id=tm.id), background_tasks=BackgroundTasks(), tenant=pm_tenant, db=db)
                raise AssertionError("a plain Project Manager must not be able to PATCH assignee_id to a Team Manager")
            except AppException as exc:
                assert exc.code == "PROJECT_MANAGER_CANNOT_ASSIGN_MEMBER", exc

            try:
                await update_task(project_task.id, TaskUpdate(assignee_id=pm.id), background_tasks=BackgroundTasks(), tenant=pm_tenant, db=db)
                raise AssertionError("a plain Project Manager must not be able to PATCH assignee_id to themselves on a delegated Team Task")
            except AppException as exc:
                assert exc.code == "PROJECT_MANAGER_CANNOT_ASSIGN_MEMBER", exc

            # A plain PM MAY, however, update an allowed core field on the
            # same task without touching assignee_id at all.
            pm_updated = await update_task(project_task.id, TaskUpdate(priority="high"), background_tasks=BackgroundTasks(), tenant=pm_tenant, db=db)
            assert pm_updated.priority == "high"
            assert pm_updated.assignee_id == assigned_to_bob.assignee_id, "an unrelated field update must never touch the existing assignee"

            # ── 28. Owner's existing assignment behavior is unaffected. ───────
            owner_reassigned = await update_task(
                project_task.id, TaskUpdate(assignee_id=bob.id),
                background_tasks=BackgroundTasks(), tenant=owner_tenant, db=db,
            )
            assert owner_reassigned.assignee_id == bob.id

            # ── Task Request conversion: the second, independently-fixed
            # surface — same invariant, different route. Full coverage of
            # the conversion_mode contract itself (self/team, forged
            # assignee, cross-role authorization, double-conversion, etc.)
            # lives in tests/test_task_request_conversion.py; this is just
            # the narrow "plain PM never names an individual" slice kept
            # alongside its sibling create_task() assertions above. ────────
            request = TaskRequest(
                organization_id=org.id, project_id=project_a.id, submitted_by_id=pm.id,
                title=f"PMTD Client Request {suffix}", status="pending",
            )
            db.add(request)
            await db.commit()
            await db.refresh(request)
            created_request_ids.append(request.id)

            try:
                await convert_task_request(
                    project_a.id, request.id,
                    TaskRequestConvert(conversion_mode="team", team_id=team_c.id),
                    background_tasks=BackgroundTasks(), tenant=pm_tenant,
                )
                raise AssertionError("converting to a team not attached to this project must be rejected")
            except AppException as exc:
                assert exc.code == "TEAM_NOT_ASSIGNABLE", exc

            converted = await convert_task_request(
                project_a.id, request.id,
                TaskRequestConvert(conversion_mode="team", team_id=team_a.id),
                background_tasks=BackgroundTasks(), tenant=pm_tenant,
            )
            assert converted.status == "converted"
            converted_task = (await db.execute(select(Task).where(Task.id == converted.converted_task_id))).scalar_one()
            created_task_ids.append(converted_task.id)
            assert converted_task.assignee_id is None, "a plain PM's team-mode conversion must never name an individual assignee"
            assert converted_task.team_id == team_a.id

        finally:
            if created_request_ids:
                await db.execute(delete(TaskRequest).where(TaskRequest.id.in_(created_request_ids)))
            await db.execute(delete(TaskTimeEntry).where(TaskTimeEntry.task_id.in_(created_task_ids)))
            await db.execute(delete(Task).where(Task.id.in_(created_task_ids)))
            await db.execute(delete(TeamMembership).where(TeamMembership.team_id.in_([team_a.id, team_c.id, other_org_team.id])))
            await db.execute(delete(ProjectTeam).where(ProjectTeam.project_id == project_a.id))
            await db.execute(delete(Team).where(Team.id.in_([team_a.id, team_c.id, other_org_team.id])))
            await db.execute(delete(ProjectMembership).where(ProjectMembership.project_id.in_([project_a.id, project_b.id])))
            await db.execute(delete(Project).where(Project.id.in_([project_a.id, project_b.id])))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id.in_([org.id, other_org.id])))
            await db.execute(delete(User).where(User.id.in_([owner.id, pm.id, tm.id, bob.id, alice.id, client_user.id])))
            await db.commit()

    await engine.dispose()


def test_project_manager_task_delegation():
    asyncio.run(_run())
