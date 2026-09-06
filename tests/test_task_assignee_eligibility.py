"""Regression tests for the Task Assignee bug-fix follow-up:
  BUG 1 — Clients must never be Task-assignable.
  BUG 2 — a Team Task's assignee dropdown/validation must be scoped to
          that exact Team's members, not the whole organization.
  BUG 3 — a Team Manager must be able to get their managed Team's
          eligible-assignee list WITHOUT organization-wide `/users` access.

Covers app.core.task_assignment.validate_task_assignee (used by both
POST /tasks and PATCH /tasks/{id}), and
GET /teams/{team_id}/assignable-users.

Covers (see the spec's "TESTS — BACKEND"):
  1. Client cannot be assigned to a Task.
  2. Client excluded even when assigning user is Owner.
  3. Client excluded even when assigning user is Admin.
  4. Team Task: Team member can be assigned.
  5. Team Task: non-Team member cannot be assigned.
  6. Team Task: member of another Team only cannot be assigned.
  7. Team Task: same-org user but not Team member cannot be assigned.
  8. Team Task: a Client who is somehow also a Team member is still
     rejected (role wins over TeamMembership).
  9. Team Manager: can assign an eligible member of their managed Team.
  10. Team Manager: cannot use an assignee from an unrelated Team.
  11. Team Manager: does not require org-wide Users permission for the
      assignable-users list.
  12. Admin: still limited to Team members for a Team Task.
  13. Owner: still limited to Team members for a Team Task.
  14. Unassigned/null remains valid.
  15. Cross-tenant user cannot be assigned.
  16. Task update validates assignee.
  17. Task creation validates assignee.
  18. Team change does not leave an invalid assignee state (Option A:
      the request is rejected, requiring the caller to explicitly supply
      a valid assignee for the new team — assignee_id is never silently
      cleared).
  19/20. Existing Project Manager / Team Manager scoped-access tests are
      run unmodified elsewhere in the suite (test_project_manager_*.py,
      test_team_manager_*.py) — this file does not touch their fixtures.

Runs against the real database connection the app uses. Every row this
test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from fastapi import BackgroundTasks
from sqlalchemy import delete

from app.api.routes.tasks import create_task, update_task
from app.api.routes.teams import list_team_assignable_users
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import CLIENT, TEAM_MANAGER, TEAM_MEMBER
from app.core.team_access import NOT_ASSIGNED as TEAM_NOT_ASSIGNED
from app.core.tenant import TenantContext
from app.models.organization import Organization, OrganizationMembership
from app.models.task import Task
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.repositories.task_repository import TaskRepository
from app.schemas.task import TaskCreate, TaskUpdate


async def _run():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]

        owner = User(full_name="TA Owner", email=f"ta.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        admin = User(full_name="TA Admin", email=f"ta.admin.{suffix}@test.invalid", hashed_password="x", role="admin")
        tm = User(full_name="TA TM", email=f"ta.tm.{suffix}@test.invalid", hashed_password="x", role=TEAM_MANAGER)
        alice = User(full_name="TA Alice", email=f"ta.alice.{suffix}@test.invalid", hashed_password="x", role=TEAM_MEMBER)
        bob = User(full_name="TA Bob", email=f"ta.bob.{suffix}@test.invalid", hashed_password="x", role=TEAM_MEMBER)
        sarah = User(full_name="TA Sarah", email=f"ta.sarah.{suffix}@test.invalid", hashed_password="x", role=TEAM_MEMBER)  # on a DIFFERENT team
        non_member = User(full_name="TA NonMember", email=f"ta.nonmember.{suffix}@test.invalid", hashed_password="x", role=TEAM_MEMBER)  # same org, no team at all
        client_user = User(full_name="TA Client", email=f"ta.client.{suffix}@test.invalid", hashed_password="x", role=TEAM_MEMBER)  # legacy/stale User.role — org role is what matters
        outsider = User(full_name="TA Outsider", email=f"ta.outsider.{suffix}@test.invalid", hashed_password="x", role="owner")
        db.add_all([owner, admin, tm, alice, bob, sarah, non_member, client_user, outsider])
        await db.commit()
        for u in (owner, admin, tm, alice, bob, sarah, non_member, client_user, outsider):
            await db.refresh(u)

        org = Organization(name=f"TA Org {suffix}", slug=f"ta-org-{suffix}", owner_id=owner.id)
        other_org = Organization(name=f"TA Other Org {suffix}", slug=f"ta-other-org-{suffix}", owner_id=outsider.id)
        db.add_all([org, other_org])
        await db.commit()
        for o in (org, other_org):
            await db.refresh(o)

        memberships = {}
        for u, role in [
            (owner, "owner"), (admin, "admin"), (tm, TEAM_MANAGER), (alice, TEAM_MEMBER),
            (bob, TEAM_MEMBER), (sarah, TEAM_MEMBER), (non_member, TEAM_MEMBER), (client_user, CLIENT),
        ]:
            m = OrganizationMembership(organization_id=org.id, user_id=u.id, role=role)
            db.add(m)
            memberships[u.id] = m
        outsider_membership = OrganizationMembership(organization_id=other_org.id, user_id=outsider.id, role="owner")
        db.add(outsider_membership)
        await db.commit()

        # Technology Team: managed by tm, members alice + bob (+ tm, added
        # automatically as manager). client_user is also (incorrectly,
        # legacy-data-style) a TeamMembership row here — Rule A must still
        # reject them purely on role, independent of that membership row.
        tech_team = Team(name=f"Technology {suffix}", team_manager_id=tm.id, created_by_id=owner.id, organization_id=org.id)
        marketing_team = Team(name=f"Marketing {suffix}", team_manager_id=owner.id, created_by_id=owner.id, organization_id=org.id)
        db.add_all([tech_team, marketing_team])
        await db.commit()
        for t in (tech_team, marketing_team):
            await db.refresh(t)

        db.add_all([
            TeamMembership(team_id=tech_team.id, user_id=tm.id),
            TeamMembership(team_id=tech_team.id, user_id=alice.id),
            TeamMembership(team_id=tech_team.id, user_id=bob.id),
            TeamMembership(team_id=tech_team.id, user_id=client_user.id),  # legacy-style bad data
            TeamMembership(team_id=marketing_team.id, user_id=owner.id),
            TeamMembership(team_id=marketing_team.id, user_id=sarah.id),
        ])
        await db.commit()

        org_id, other_org_id = org.id, other_org.id
        tech_team_id, marketing_team_id = tech_team.id, marketing_team.id
        user_ids = [owner.id, admin.id, tm.id, alice.id, bob.id, sarah.id, non_member.id, client_user.id, outsider.id]

        owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[owner.id], user=owner, db=db)
        admin_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[admin.id], user=admin, db=db)
        tm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[tm.id], user=tm, db=db)

        created_task_ids: list[int] = []

        try:
            # ── 17. Task creation validates assignee — 4. Team member can
            # be assigned to a Team Task. ─────────────────────────────────────
            task = await create_task(
                TaskCreate(name=f"TA Task {suffix}", team_id=tech_team_id, assignee_id=alice.id),
                background_tasks=BackgroundTasks(), tenant=owner_tenant, db=db,
            )
            created_task_ids.append(task.id)
            assert task.assignee_id == alice.id

            # ── 1, 2. Client cannot be assigned — even by Owner. ──────────────
            try:
                await create_task(
                    TaskCreate(name=f"TA Bad Client Task {suffix}", team_id=tech_team_id, assignee_id=client_user.id),
                    background_tasks=BackgroundTasks(), tenant=owner_tenant, db=db,
                )
                raise AssertionError("a Client must never be assignable, even for the Owner")
            except AppException as exc:
                assert exc.status_code == 400, exc
                assert exc.code == "CLIENT_NOT_ASSIGNABLE"

            # ── 3. Client excluded even when assigning user is Admin. ────────
            try:
                await create_task(
                    TaskCreate(name=f"TA Bad Client Task 2 {suffix}", team_id=tech_team_id, assignee_id=client_user.id),
                    background_tasks=BackgroundTasks(), tenant=admin_tenant, db=db,
                )
                raise AssertionError("a Client must never be assignable, even for an Admin")
            except AppException as exc:
                assert exc.status_code == 400, exc
                assert exc.code == "CLIENT_NOT_ASSIGNABLE"

            # ── 8. A Client who is ALSO (incorrectly) a TeamMembership
            # member of the task's team is still rejected on role alone. ─────
            try:
                await create_task(
                    TaskCreate(name=f"TA Bad Client Team Task {suffix}", team_id=tech_team_id, assignee_id=client_user.id),
                    background_tasks=BackgroundTasks(), tenant=tm_tenant, db=db,
                )
                raise AssertionError("Client role must win over an existing (bad legacy) TeamMembership row")
            except AppException as exc:
                assert exc.code == "CLIENT_NOT_ASSIGNABLE", exc

            # ── 6. Member of ANOTHER team only cannot be assigned — Sarah is
            # on Marketing, not Technology. ────────────────────────────────────
            try:
                await create_task(
                    TaskCreate(name=f"TA Bad Cross Team Task {suffix}", team_id=tech_team_id, assignee_id=sarah.id),
                    background_tasks=BackgroundTasks(), tenant=owner_tenant, db=db,
                )
                raise AssertionError("a member of a DIFFERENT team only must not be assignable to this team's task")
            except AppException as exc:
                assert exc.status_code == 400, exc
                assert exc.code == "ASSIGNEE_NOT_TEAM_MEMBER"

            # ── 7. Same-org user but not a member of ANY team cannot be
            # assigned to a Team Task. ────────────────────────────────────────
            try:
                await create_task(
                    TaskCreate(name=f"TA Bad NonMember Task {suffix}", team_id=tech_team_id, assignee_id=non_member.id),
                    background_tasks=BackgroundTasks(), tenant=owner_tenant, db=db,
                )
                raise AssertionError("a same-org user with no team membership at all must not be assignable to a Team Task")
            except AppException as exc:
                assert exc.code == "ASSIGNEE_NOT_TEAM_MEMBER", exc

            # ── 5. non-Team member cannot be assigned — same case as 7,
            # verified via the Admin actor too (12. Admin still limited to
            # Team members). ───────────────────────────────────────────────────
            try:
                await create_task(
                    TaskCreate(name=f"TA Bad Admin NonMember Task {suffix}", team_id=tech_team_id, assignee_id=non_member.id),
                    background_tasks=BackgroundTasks(), tenant=admin_tenant, db=db,
                )
                raise AssertionError("Admin power must not expand the Team Task's valid assignee set")
            except AppException as exc:
                assert exc.code == "ASSIGNEE_NOT_TEAM_MEMBER", exc

            # ── 13. Owner still limited to Team members for a Team Task
            # (Sarah case above already used owner_tenant — reconfirm with
            # a plain non-member too). ──────────────────────────────────────
            try:
                await create_task(
                    TaskCreate(name=f"TA Bad Owner NonMember Task {suffix}", team_id=tech_team_id, assignee_id=non_member.id),
                    background_tasks=BackgroundTasks(), tenant=owner_tenant, db=db,
                )
                raise AssertionError("Owner power must not expand the Team Task's valid assignee set")
            except AppException as exc:
                assert exc.code == "ASSIGNEE_NOT_TEAM_MEMBER", exc

            # ── 15. Cross-tenant user cannot be assigned (create). ────────────
            try:
                await create_task(
                    TaskCreate(name=f"TA Bad Outsider Task {suffix}", assignee_id=outsider.id),
                    background_tasks=BackgroundTasks(), tenant=owner_tenant, db=db,
                )
                raise AssertionError("a user from a different organization must never be assignable")
            except AppException as exc:
                assert exc.code == "INVALID_ASSIGNEE", exc

            # ── 14. Unassigned/null remains valid. ────────────────────────────
            unassigned_task = await create_task(
                TaskCreate(name=f"TA Unassigned Task {suffix}", team_id=tech_team_id, assignee_id=None),
                background_tasks=BackgroundTasks(), tenant=owner_tenant, db=db,
            )
            created_task_ids.append(unassigned_task.id)
            assert unassigned_task.assignee_id is None

            # ── 9. Team Manager can assign an eligible member of their
            # managed team. ───────────────────────────────────────────────────
            tm_task = await create_task(
                TaskCreate(name=f"TA TM Task {suffix}", team_id=tech_team_id, assignee_id=bob.id),
                background_tasks=BackgroundTasks(), tenant=tm_tenant, db=db,
            )
            created_task_ids.append(tm_task.id)
            assert tm_task.assignee_id == bob.id

            # ── 10. Team Manager cannot use an assignee from an unrelated
            # team (tm does not manage/belong to Marketing). ──────────────────
            try:
                await create_task(
                    TaskCreate(name=f"TA TM Bad Task {suffix}", team_id=tech_team_id, assignee_id=sarah.id),
                    background_tasks=BackgroundTasks(), tenant=tm_tenant, db=db,
                )
                raise AssertionError("Team Manager must not be able to assign a member of an unrelated team")
            except AppException as exc:
                assert exc.code == "ASSIGNEE_NOT_TEAM_MEMBER", exc

            # ── 16, 17 (update side). PATCH validates assignee too. ───────────
            try:
                await update_task(
                    task.id, TaskUpdate(assignee_id=client_user.id),
                    background_tasks=BackgroundTasks(), tenant=owner_tenant, db=db,
                )
                raise AssertionError("PATCH must validate assignee_id exactly like create")
            except AppException as exc:
                assert exc.code == "CLIENT_NOT_ASSIGNABLE", exc
            try:
                await update_task(
                    task.id, TaskUpdate(assignee_id=sarah.id),
                    background_tasks=BackgroundTasks(), tenant=owner_tenant, db=db,
                )
                raise AssertionError("PATCH must reject a non-team-member assignee on a Team Task")
            except AppException as exc:
                assert exc.code == "ASSIGNEE_NOT_TEAM_MEMBER", exc
            # A valid update still succeeds.
            updated = await update_task(
                task.id, TaskUpdate(assignee_id=bob.id),
                background_tasks=BackgroundTasks(), tenant=owner_tenant, db=db,
            )
            assert updated.assignee_id == bob.id

            # ── 18. Team change must not leave an invalid assignee state:
            # task is currently on tech_team with assignee=bob (a tech
            # member, not eligible for marketing_team) — moving it to
            # marketing_team without also fixing the assignee must be
            # rejected outright, never silently clearing assignee_id. ────────
            try:
                await update_task(
                    task.id, TaskUpdate(team_id=marketing_team_id),
                    background_tasks=BackgroundTasks(), tenant=owner_tenant, db=db,
                )
                raise AssertionError("moving a Task to a new Team must not silently leave an invalid assignee in place")
            except AppException as exc:
                assert exc.code == "ASSIGNEE_NOT_TEAM_MEMBER", exc
            # confirm nothing was mutated by the rejected attempt.
            still_tech = await TaskRepository(db, org_id).get_by_id(task.id)
            assert still_tech.team_id == tech_team_id, "a rejected team-change request must not partially apply"
            assert still_tech.assignee_id == bob.id, "assignee_id must never be silently cleared by a rejected team-change"
            # The caller CAN move it by supplying a valid assignee for the
            # new team in the same request (Option A from the spec).
            moved = await update_task(
                task.id, TaskUpdate(team_id=marketing_team_id, assignee_id=sarah.id),
                background_tasks=BackgroundTasks(), tenant=owner_tenant, db=db,
            )
            assert moved.team_id == marketing_team_id
            assert moved.assignee_id == sarah.id

            # ── 11. Team Manager assignable-users list does not require
            # org-wide Users permission — it's a plain get_tenant_context
            # dependency, never require_org_admin. ────────────────────────────
            tm_managed_list = await list_team_assignable_users(tech_team_id, tenant=tm_tenant)
            tm_managed_ids = {m.id for m in tm_managed_list}
            assert alice.id in tm_managed_ids
            assert bob.id in tm_managed_ids
            assert tm.id in tm_managed_ids, "the manager themself (also a TeamMembership row) must be included"
            assert client_user.id not in tm_managed_ids, "Client must never appear in the assignable list, even with a legacy TeamMembership row"
            assert sarah.id not in tm_managed_ids, "a different team's member must not appear"
            assert non_member.id not in tm_managed_ids

            # ── 10 (continued). Unrelated team: tm does not manage/belong
            # to Marketing -> must be denied, not an empty-but-200 list. ──────
            try:
                await list_team_assignable_users(marketing_team_id, tenant=tm_tenant)
                raise AssertionError("a Team Manager must not get another team's assignable-user data at all")
            except AppException as exc:
                assert exc.code == TEAM_NOT_ASSIGNED.code or exc.status_code == 403, exc

            # Owner/Admin: allowed for any team.
            owner_list = await list_team_assignable_users(tech_team_id, tenant=owner_tenant)
            assert {m.id for m in owner_list} == tm_managed_ids
            admin_list = await list_team_assignable_users(marketing_team_id, tenant=admin_tenant)
            assert {m.id for m in admin_list} == {owner.id, sarah.id}

        finally:
            if created_task_ids:
                await db.execute(delete(Task).where(Task.id.in_(created_task_ids)))
            await db.execute(delete(TeamMembership).where(TeamMembership.team_id.in_([tech_team_id, marketing_team_id])))
            await db.execute(delete(Team).where(Team.id.in_([tech_team_id, marketing_team_id])))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_([org_id, other_org_id])))
            await db.execute(delete(Organization).where(Organization.id.in_([org_id, other_org_id])))
            await db.execute(delete(User).where(User.id.in_(user_ids)))
            await db.commit()

    await engine.dispose()


def test_task_assignee_eligibility():
    asyncio.run(_run())
