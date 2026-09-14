"""Regression tests for the Edit-Task-flow follow-up: a Team Manager
converting their own self-owned Personal Task (team_id NULL, they ARE the
assignee) into one of their managed Teams' Tasks, in a SINGLE PATCH that
sets both `team_id` and `assignee_id` together — no Save-then-reopen
round trip required.

ROOT CAUSE (UX issue): the Assignee field's editability/options used to
be computed only from the read-only field whitelist for a pure Personal
Task owner (PERSONAL_TASK_OWNER_ALLOWED_FIELDS never includes
assignee_id) — there was no atomic-transition exception, so a Personal
Task owner could set `team_id` but including `assignee_id` in the SAME
request was always rejected outright, forcing a save, reopen (now a
managed-Team-Task editor), then a second PATCH.

FIX: `is_personal_to_managed_team_transition` in `update_task()` — narrowly
scoped: only when `team_id` is present and non-None in THIS payload AND
the caller manages that EXACT target team (`team_repo.is_manager`, not
merely `has_access`). Grants inclusion of `assignee_id` for that one
request only; the target assignee's actual eligibility (Client/inactive/
cross-tenant/wrong-team) is still fully re-validated by the existing,
unmodified `validate_task_assignee`. Generic Personal Task owners still
never get `assignee_id` — see test_personal_task_ownership_permissions.py
and test_pm_personal_task_and_edit_payload.py, both unaffected.

Covers spec test items 3, 5, 6, 7, 8, 9, 10, 11, 12.

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

from app.api.routes.tasks import start_task_timer, update_task
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import CLIENT, OWNER, TEAM_MANAGER, TEAM_MEMBER
from app.core.tenant import TenantContext
from app.models.organization import Organization, OrganizationMembership
from app.models.task import Task
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.schemas.task import TaskUpdate


async def _run():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]

        owner = User(full_name="PTT Owner", email=f"ptt.owner.{suffix}@example-corp.com", hashed_password="x", role="owner")
        tm = User(full_name="PTT TM", email=f"ptt.tm.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MANAGER)
        tm_other = User(full_name="PTT TM Other", email=f"ptt.tmother.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MANAGER)
        eligible_member = User(full_name="PTT Eligible", email=f"ptt.eligible.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)
        wrong_team_member = User(full_name="PTT WrongTeam", email=f"ptt.wrongteam.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)
        client_user = User(full_name="PTT Client", email=f"ptt.client.{suffix}@example-corp.com", hashed_password="x", role=CLIENT)
        inactive_member = User(full_name="PTT Inactive", email=f"ptt.inactive.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)
        db.add_all([owner, tm, tm_other, eligible_member, wrong_team_member, client_user, inactive_member])
        await db.commit()
        for u in (owner, tm, tm_other, eligible_member, wrong_team_member, client_user, inactive_member):
            await db.refresh(u)

        org = Organization(name=f"PTT Org {suffix}", slug=f"ptt-org-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.commit()
        org = (await db.execute(select(Organization).options(selectinload(Organization.subscription)).where(Organization.id == org.id))).scalar_one()

        # A second, isolated org, for the cross-tenant assignee probe.
        other_org_owner = User(full_name="PTT Other Org Owner", email=f"ptt.otherowner.{suffix}@example-corp.com", hashed_password="x", role="owner")
        db.add(other_org_owner)
        await db.commit()
        await db.refresh(other_org_owner)
        other_org = Organization(name=f"PTT Other Org {suffix}", slug=f"ptt-other-org-{suffix}", owner_id=other_org_owner.id)
        db.add(other_org)
        await db.commit()
        other_org = (await db.execute(select(Organization).options(selectinload(Organization.subscription)).where(Organization.id == other_org.id))).scalar_one()
        cross_tenant_user = User(full_name="PTT CrossTenant", email=f"ptt.crosstenant.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)
        db.add(cross_tenant_user)
        await db.commit()
        await db.refresh(cross_tenant_user)
        db.add(OrganizationMembership(organization_id=other_org.id, user_id=cross_tenant_user.id, role=TEAM_MEMBER))
        await db.commit()

        memberships = {}
        for u, role in [
            (owner, OWNER), (tm, TEAM_MANAGER), (tm_other, TEAM_MANAGER),
            (eligible_member, TEAM_MEMBER), (wrong_team_member, TEAM_MEMBER),
            (client_user, CLIENT), (inactive_member, TEAM_MEMBER),
        ]:
            m = OrganizationMembership(organization_id=org.id, user_id=u.id, role=role)
            db.add(m)
            memberships[u.id] = m
        await db.commit()
        inactive_membership = memberships[inactive_member.id]
        inactive_membership.is_active = False
        await db.commit()

        # TM's managed team, with an eligible member on it.
        team_managed = Team(name=f"PTT Managed {suffix}", team_manager_id=tm.id, created_by_id=owner.id, organization_id=org.id)
        # A team TM does NOT manage (managed by tm_other) — the "unmanaged
        # Team" probe.
        team_unmanaged = Team(name=f"PTT Unmanaged {suffix}", team_manager_id=tm_other.id, created_by_id=owner.id, organization_id=org.id)
        # A second managed-by-TM team, for the "wrong-team member" probe
        # (wrong_team_member belongs here, not to team_managed).
        team_managed_2 = Team(name=f"PTT Managed 2 {suffix}", team_manager_id=tm.id, created_by_id=owner.id, organization_id=org.id)
        db.add_all([team_managed, team_unmanaged, team_managed_2])
        await db.commit()
        for t in (team_managed, team_unmanaged, team_managed_2):
            await db.refresh(t)
        db.add_all([
            TeamMembership(team_id=team_managed.id, user_id=eligible_member.id),
            TeamMembership(team_id=team_managed.id, user_id=inactive_member.id),
            TeamMembership(team_id=team_managed_2.id, user_id=wrong_team_member.id),
        ])
        await db.commit()

        tm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[tm.id], user=tm, db=db)

        created_task_ids: list[int] = []

        async def _make_personal_task(*, title) -> Task:
            task = Task(name=title, team_id=None, project_id=None, assignee_id=tm.id, created_by_id=tm.id, organization_id=org.id, status="todo", priority="medium")
            db.add(task)
            await db.commit()
            await db.refresh(task)
            created_task_ids.append(task.id)
            return task

        async def _update(task_id, payload, tenant=tm_tenant):
            return await update_task(task_id, payload, background_tasks=BackgroundTasks(), tenant=tenant, db=db)

        try:
            # ── 3/4. TM selects a managed Team + an eligible member,
            # saves ONCE — both fields persist atomically, no reopen. ─────
            t1 = await _make_personal_task(title=f"PTT Task 1 {suffix}")
            updated1 = await _update(t1.id, TaskUpdate(team_id=team_managed.id, assignee_id=eligible_member.id))
            assert updated1.team_id == team_managed.id
            assert updated1.assignee_id == eligible_member.id

            # ── 5. TM selects Team but keeps self -> succeeds (TM is
            # eligible: they manage the team). ──────────────────────────
            t2 = await _make_personal_task(title=f"PTT Task 2 {suffix}")
            updated2 = await _update(t2.id, TaskUpdate(team_id=team_managed.id, assignee_id=tm.id))
            assert updated2.team_id == team_managed.id
            assert updated2.assignee_id == tm.id

            # ── 6. TM selects Unassigned -> succeeds (Team Tasks may
            # legitimately be Unassigned). ──────────────────────────────
            t3 = await _make_personal_task(title=f"PTT Task 3 {suffix}")
            updated3 = await _update(t3.id, TaskUpdate(team_id=team_managed.id, assignee_id=None))
            assert updated3.team_id == team_managed.id
            assert updated3.assignee_id is None

            # ── 7. Unmanaged Team (TM doesn't manage it) + assignee_id in
            # the same PATCH -> rejected outright (field-forbidden, since
            # the atomic-transition exception never activates without
            # `is_manager` on the exact target team). ────────────────────
            t4 = await _make_personal_task(title=f"PTT Task 4 {suffix}")
            try:
                await _update(t4.id, TaskUpdate(team_id=team_unmanaged.id, assignee_id=eligible_member.id))
                raise AssertionError("a Personal Task owner must not set assignee_id while moving into a Team they don't manage")
            except AppException as exc:
                assert exc.code == "PERSONAL_TASK_FIELD_FORBIDDEN", exc
            unchanged4 = (await db.execute(select(Task).where(Task.id == t4.id))).scalar_one()
            assert unchanged4.team_id is None, "the whole PATCH must be refused, not partially applied"

            # ── 8. Wrong-team member (belongs to team_managed_2, not
            # team_managed) -> rejected by validate_task_assignee. ────────
            t5 = await _make_personal_task(title=f"PTT Task 5 {suffix}")
            try:
                await _update(t5.id, TaskUpdate(team_id=team_managed.id, assignee_id=wrong_team_member.id))
                raise AssertionError("an assignee who isn't a member of the target Team must be rejected")
            except AppException as exc:
                assert exc.code == "ASSIGNEE_NOT_TEAM_MEMBER", exc

            # ── 9a. Client assignee -> rejected. ────────────────────────
            t6 = await _make_personal_task(title=f"PTT Task 6 {suffix}")
            try:
                await _update(t6.id, TaskUpdate(team_id=team_managed.id, assignee_id=client_user.id))
                raise AssertionError("a Client must never be assignable, even via this transition")
            except AppException as exc:
                assert exc.code == "CLIENT_NOT_ASSIGNABLE", exc

            # ── 9b. Inactive member -> rejected. ────────────────────────
            t7 = await _make_personal_task(title=f"PTT Task 7 {suffix}")
            try:
                await _update(t7.id, TaskUpdate(team_id=team_managed.id, assignee_id=inactive_member.id))
                raise AssertionError("an inactive membership must never be assignable")
            except AppException as exc:
                assert exc.code == "INVALID_ASSIGNEE", exc

            # ── 9c. Cross-tenant user -> rejected. ──────────────────────
            t8 = await _make_personal_task(title=f"PTT Task 8 {suffix}")
            try:
                await _update(t8.id, TaskUpdate(team_id=team_managed.id, assignee_id=cross_tenant_user.id))
                raise AssertionError("a user with no membership in this organization must never be assignable")
            except AppException as exc:
                assert exc.code == "INVALID_ASSIGNEE", exc

            # ── 10. Active timer reassignment safeguard remains intact:
            # TM starts their own timer on the Personal Task, then tries
            # to move it to a Team with a DIFFERENT assignee in the same
            # request -> rejected; timer must be stopped first. ──────────
            t9 = await _make_personal_task(title=f"PTT Task 9 {suffix}")
            await start_task_timer(t9.id, tenant=tm_tenant)
            try:
                await _update(t9.id, TaskUpdate(team_id=team_managed.id, assignee_id=eligible_member.id))
                raise AssertionError("reassigning away from the active timer's owner must be rejected while it's running")
            except AppException as exc:
                assert exc.code == "TASK_TIMER_ACTIVE", exc
            # Stop the timer, then the same transition succeeds.
            from app.api.routes.tasks import stop_task_timer
            await stop_task_timer(t9.id, tenant=tm_tenant)
            updated9 = await _update(t9.id, TaskUpdate(team_id=team_managed.id, assignee_id=eligible_member.id))
            assert updated9.assignee_id == eligible_member.id

            # ── 11. Personal Task with no Team still cannot arbitrarily
            # change assignee (team_id omitted/absent from the PATCH). ────
            t10 = await _make_personal_task(title=f"PTT Task 10 {suffix}")
            try:
                await _update(t10.id, TaskUpdate(assignee_id=eligible_member.id))
                raise AssertionError("assignee_id must stay protected while team_id is not part of the same PATCH")
            except AppException as exc:
                assert exc.code == "PERSONAL_TASK_FIELD_FORBIDDEN", exc
            # Also rejected even if team_id is explicitly sent as still-None.
            try:
                await _update(t10.id, TaskUpdate(team_id=None, assignee_id=eligible_member.id))
                raise AssertionError("assignee_id must stay protected when team_id is explicitly re-sent as still NULL")
            except AppException as exc:
                assert exc.code == "PERSONAL_TASK_FIELD_FORBIDDEN", exc

            # ── 12. Existing managed-Team-Task edit behavior is unaffected
            # AFTER the transition completes — reopening t1 (now a normal
            # managed-Team Task) lets the TM reassign freely via the
            # ordinary Team Manager path, no special-casing needed. ───────
            reassigned = await _update(t1.id, TaskUpdate(assignee_id=None))
            assert reassigned.assignee_id is None
            reassigned_back = await _update(t1.id, TaskUpdate(assignee_id=eligible_member.id))
            assert reassigned_back.assignee_id == eligible_member.id

        finally:
            if created_task_ids:
                await db.execute(delete(Task).where(Task.id.in_(created_task_ids)))
            await db.execute(delete(TeamMembership).where(TeamMembership.team_id.in_([team_managed.id, team_unmanaged.id, team_managed_2.id])))
            await db.execute(delete(Team).where(Team.id.in_([team_managed.id, team_unmanaged.id, team_managed_2.id])))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_([org.id, other_org.id])))
            await db.execute(delete(Organization).where(Organization.id.in_([org.id, other_org.id])))
            await db.execute(delete(User).where(User.id.in_([
                owner.id, tm.id, tm_other.id, eligible_member.id, wrong_team_member.id,
                client_user.id, inactive_member.id, other_org_owner.id, cross_tenant_user.id,
            ])))
            await db.commit()

    await engine.dispose()


def test_personal_task_to_managed_team_transition():
    asyncio.run(_run())
