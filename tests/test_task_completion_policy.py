"""Regression tests for the Task-completion-policy follow-up:
`can_complete_task_directly` — eligible Team Managers/Project Managers may
complete THEIR OWN scoped Task straight to Done; ordinary Team Members (and
TM/PM acting as a mere ordinary assignee outside their own managed scope)
continue through Pending Review.

ROOT CAUSE (both fixed here, in the same canonical helper, used by BOTH
`update_task_status` and `update_task` so they can never drift):

  1. `update_task_status` used to branch entirely on the literal
     `tenant.org_role == TEAM_MEMBER` string. Since a Team Manager's own
     primary org_role is never literally "team_member", a TM who was
     simply the ORDINARY assignee of a Task under a Team they do NOT
     manage (or a PM under a Project they do NOT manage) fell straight
     into the generic "Marked done directly." branch — an unintended,
     unscoped self-approval bypass. Fixed items 4 and 7 below.
  2. The same literal check ALSO incorrectly forced a plain Team Member's
     own PERSONAL Task (team_id NULL — no Team reviewer exists at all)
     through `pending_review`, even though it should always complete
     directly (no role restriction on Personal Task ownership). Fixed
     implicitly by items 3/6 applying regardless of the owner's role.
  3. `update_task` (PATCH /tasks/{id}) had no review-downgrade logic
     whatsoever — ANY caller who passed the field whitelist (bare
     assignee, plain PM, etc.) got unconditional "Marked done directly.",
     including a plain PM who was merely the bare assignee of an
     unrelated, unmanaged Project's Task. Fixed by item 7's mirrored
     coverage against this endpoint too.

Covers spec TEST 1-11 (TEST 12, the dnd-kit Board scenario, is a pure UI/
interaction concern with no distinct backend policy of its own — the Board
already funnels every drag through this same `taskApi.updateStatus`/
`update()` path, so it needs no separate backend test).

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

from app.api.routes.tasks import approve_task, update_task, update_task_status
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import OWNER, PROJECT_MANAGER, TEAM_MANAGER, TEAM_MEMBER
from app.core.tenant import TenantContext
from app.models.organization import Organization, OrganizationMembership
from app.models.project import Project, ProjectMembership, ProjectTeam
from app.models.task import Task
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.schemas.task import TaskStatusUpdate, TaskUpdate


async def _run():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]

        owner = User(full_name="TCP Owner", email=f"tcp.owner.{suffix}@example-corp.com", hashed_password="x", role="owner")
        tm = User(full_name="TCP TM", email=f"tcp.tm.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MANAGER)
        other_tm = User(full_name="TCP Other TM", email=f"tcp.othertm.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MANAGER)
        pm = User(full_name="TCP PM", email=f"tcp.pm.{suffix}@example-corp.com", hashed_password="x", role=PROJECT_MANAGER)
        member = User(full_name="TCP Member", email=f"tcp.member.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)
        db.add_all([owner, tm, other_tm, pm, member])
        await db.commit()
        for u in (owner, tm, other_tm, pm, member):
            await db.refresh(u)

        org = Organization(name=f"TCP Org {suffix}", slug=f"tcp-org-{suffix}", owner_id=owner.id)
        other_org = Organization(name=f"TCP Other Org {suffix}", slug=f"tcp-other-org-{suffix}", owner_id=owner.id)
        db.add_all([org, other_org])
        await db.commit()
        org = (await db.execute(select(Organization).options(selectinload(Organization.subscription)).where(Organization.id == org.id))).scalar_one()

        memberships = {}
        for u, role in [(owner, OWNER), (tm, TEAM_MANAGER), (other_tm, TEAM_MANAGER), (pm, PROJECT_MANAGER), (member, TEAM_MEMBER)]:
            m = OrganizationMembership(organization_id=org.id, user_id=u.id, role=role)
            db.add(m)
            memberships[u.id] = m
        await db.commit()

        # Technology Team: managed by `tm`. Marketing Team: managed by
        # `other_tm` — `tm` does NOT manage it (Test 4's negative case).
        technology = Team(name=f"TCP Technology {suffix}", team_manager_id=tm.id, created_by_id=owner.id, organization_id=org.id)
        marketing = Team(name=f"TCP Marketing {suffix}", team_manager_id=other_tm.id, created_by_id=owner.id, organization_id=org.id)
        db.add_all([technology, marketing])
        await db.commit()
        for t in (technology, marketing):
            await db.refresh(t)
        db.add_all([
            TeamMembership(team_id=technology.id, user_id=tm.id),
            TeamMembership(team_id=technology.id, user_id=member.id),
            TeamMembership(team_id=marketing.id, user_id=other_tm.id),
            TeamMembership(team_id=marketing.id, user_id=tm.id),  # tm is a plain MEMBER of Marketing, not its manager
        ])
        await db.commit()

        # Clarvs: managed by `pm`. Unrelated Project: NOT managed by `pm`
        # (Test 7's negative case).
        clarvs = Project(name=f"TCP Clarvs {suffix}", created_by_id=owner.id, organization_id=org.id, status="active")
        unrelated_project = Project(name=f"TCP Unrelated {suffix}", created_by_id=owner.id, organization_id=org.id, status="active")
        db.add_all([clarvs, unrelated_project])
        await db.commit()
        for p in (clarvs, unrelated_project):
            await db.refresh(p)
        db.add(ProjectMembership(project_id=clarvs.id, user_id=pm.id))
        await db.commit()
        # PM-delegation scenario (Test 8): Clarvs -> Technology Team.
        db.add(ProjectTeam(project_id=clarvs.id, team_id=technology.id, assigned_by_id=owner.id))
        await db.commit()

        owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[owner.id], user=owner, db=db)
        tm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[tm.id], user=tm, db=db)
        pm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[pm.id], user=pm, db=db)
        member_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[member.id], user=member, db=db)

        created_task_ids: list[int] = []

        async def _make_task(*, team_id=None, project_id=None, assignee_id) -> Task:
            task = Task(
                name=f"TCP Task {uuid.uuid4().hex[:6]}", team_id=team_id, project_id=project_id,
                assignee_id=assignee_id, created_by_id=owner.id, organization_id=org.id, status="in_progress",
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)
            created_task_ids.append(task.id)
            return task

        async def _set_status(task_id, status, tenant, *, via_update=False):
            if via_update:
                return await update_task(task_id, TaskUpdate(status=status), background_tasks=BackgroundTasks(), tenant=tenant, db=db)
            return await update_task_status(task_id, TaskStatusUpdate(status=status), background_tasks=BackgroundTasks(), tenant=tenant, db=db)

        try:
            # ── TEST 1 — regular Team Member on a Team Task -> Pending
            # Review, not direct Done. Verified via BOTH endpoints (the
            # spec's own "direct API calls must not bypass this" rule). ──
            for via_update in (False, True):
                t1 = await _make_task(team_id=technology.id, assignee_id=member.id)
                result = await _set_status(t1.id, "done", member_tenant, via_update=via_update)
                assert result.status == "pending_review", f"regular Team Member must go to Pending Review (via_update={via_update})"
                assert result.completed_by_id == member.id
                assert result.reviewed_by_id is None
                assert result.review_note is None

            # ── TEST 2 — TM own managed-Team Task -> Done directly, no
            # self-review, verified via both endpoints. ─────────────────
            for via_update in (False, True):
                t2 = await _make_task(team_id=technology.id, assignee_id=tm.id)
                result = await _set_status(t2.id, "done", tm_tenant, via_update=via_update)
                assert result.status == "done", f"TM completing their own managed-Team Task must go straight to Done (via_update={via_update})"
                assert result.review_note == "Marked done directly."
                assert result.reviewed_by_id == tm.id

            # ── TEST 3 — TM Personal Task -> Done directly. ─────────────
            for via_update in (False, True):
                t3 = await _make_task(team_id=None, assignee_id=tm.id)
                result = await _set_status(t3.id, "done", tm_tenant, via_update=via_update)
                assert result.status == "done", f"TM's own Personal Task must go straight to Done (via_update={via_update})"
                assert result.review_note == "Marked done directly."

            # ── TEST 4 — TM assigned in an UNMANAGED Team (Marketing,
            # managed by other_tm) -> ordinary review workflow, TM title
            # alone must not grant direct completion. ──────────────────
            for via_update in (False, True):
                t4 = await _make_task(team_id=marketing.id, assignee_id=tm.id)
                result = await _set_status(t4.id, "done", tm_tenant, via_update=via_update)
                assert result.status == "pending_review", f"a TM who is merely an ordinary assignee elsewhere must NOT get direct-Done (via_update={via_update})"
                assert result.reviewed_by_id is None

            # ── TEST 5 — PM own managed-Project Task -> Done directly. ───
            for via_update in (False, True):
                t5 = await _make_task(project_id=clarvs.id, assignee_id=pm.id)
                result = await _set_status(t5.id, "done", pm_tenant, via_update=via_update)
                assert result.status == "done", f"PM completing their own managed-Project Task must go straight to Done (via_update={via_update})"
                assert result.review_note == "Marked done directly."

            # ── TEST 6 — PM Personal Task (project may be NULL or
            # managed) -> Done directly. ─────────────────────────────────
            for via_update in (False, True):
                t6a = await _make_task(team_id=None, project_id=None, assignee_id=pm.id)
                result = await _set_status(t6a.id, "done", pm_tenant, via_update=via_update)
                assert result.status == "done", f"PM's own project-less Personal Task must go straight to Done (via_update={via_update})"
                t6b = await _make_task(team_id=None, project_id=clarvs.id, assignee_id=pm.id)
                result = await _set_status(t6b.id, "done", pm_tenant, via_update=via_update)
                assert result.status == "done", f"PM's own Personal Task with a managed Project reference must go straight to Done (via_update={via_update})"

            # ── TEST 7 — PM assigned to an UNRELATED, unmanaged Project's
            # Task -> ordinary review workflow, PM title alone must not
            # grant organization-wide direct-Done authority. `team_id` is
            # deliberately set here (a Team the PM doesn't manage either)
            # — a project-less Task would instead be the PM's own valid
            # Personal Task (see TEST 6), a genuinely different case; this
            # isolates "assigned under an unmanaged Project" specifically. ──
            for via_update in (False, True):
                t7 = await _make_task(team_id=marketing.id, project_id=unrelated_project.id, assignee_id=pm.id)
                result = await _set_status(t7.id, "done", pm_tenant, via_update=via_update)
                assert result.status == "pending_review", f"a PM who is merely an ordinary assignee on an unmanaged Project must NOT get direct-Done (via_update={via_update})"

            # ── TEST 8 — PM-delegated Team Task (Clarvs -> Technology
            # Team, assignee = a plain Team Member) -> the ordinary Team
            # Member review workflow applies, and ONLY the Team Manager
            # reviews (via the existing `approve_task` — never a second,
            # PM-level review layer). ────────────────────────────────────
            t8 = await _make_task(team_id=technology.id, project_id=clarvs.id, assignee_id=member.id)
            submitted = await update_task_status(t8.id, TaskStatusUpdate(status="done"), background_tasks=BackgroundTasks(), tenant=member_tenant, db=db)
            assert submitted.status == "pending_review"
            approved = await approve_task(t8.id, background_tasks=BackgroundTasks(), tenant=tm_tenant, db=db)
            assert approved.status == "done"
            assert approved.review_note == "Approved"

            # A Project Manager (not the Team's manager) must NOT be able
            # to short-circuit this via a second review — approve_task
            # itself already denies a plain PM (require_task_manage_access
            # never grants PM authority at all); reconfirmed here.
            t8b = await _make_task(team_id=technology.id, project_id=clarvs.id, assignee_id=member.id)
            await update_task_status(t8b.id, TaskStatusUpdate(status="done"), background_tasks=BackgroundTasks(), tenant=member_tenant, db=db)
            try:
                await approve_task(t8b.id, background_tasks=BackgroundTasks(), tenant=pm_tenant, db=db)
                raise AssertionError("a Project Manager must never approve a delegated Team Task themselves")
            except Exception as exc:
                assert getattr(exc, "status_code", None) == 403, exc

            # ── TEST 9 — TM reviews another member's Task -> existing
            # Approve flow unchanged (reconfirmed here for completeness;
            # exhaustively covered elsewhere too). ──────────────────────
            t9 = await _make_task(team_id=technology.id, assignee_id=member.id)
            await update_task_status(t9.id, TaskStatusUpdate(status="done"), background_tasks=BackgroundTasks(), tenant=member_tenant, db=db)
            approved9 = await approve_task(t9.id, background_tasks=BackgroundTasks(), tenant=tm_tenant, db=db)
            assert approved9.status == "done"
            assert approved9.review_note == "Approved"

            # ── TEST 10 — Owner/Admin direct completion unchanged. ───────
            for via_update in (False, True):
                t10 = await _make_task(team_id=technology.id, assignee_id=member.id)
                result = await _set_status(t10.id, "done", owner_tenant, via_update=via_update)
                assert result.status == "done", f"Owner/Admin direct completion must remain unchanged (via_update={via_update})"
                assert result.review_note == "Marked done directly."

            # ── TEST 11 — cross-tenant: a TM's identical eligibility
            # logic must never leak into another organization's Task. ────
            cross_owner = User(full_name="TCP Cross Owner", email=f"tcp.crossowner.{suffix}@example-corp.com", hashed_password="x", role="owner")
            db.add(cross_owner)
            await db.commit()
            await db.refresh(cross_owner)
            cross_membership = OrganizationMembership(organization_id=other_org.id, user_id=cross_owner.id, role="owner")
            db.add(cross_membership)
            await db.commit()
            cross_team = Team(name=f"TCP Cross Team {suffix}", team_manager_id=cross_owner.id, created_by_id=cross_owner.id, organization_id=other_org.id)
            db.add(cross_team)
            await db.commit()
            await db.refresh(cross_team)
            cross_task = Task(
                name=f"TCP Cross Task {suffix}", team_id=cross_team.id, assignee_id=cross_owner.id,
                created_by_id=cross_owner.id, organization_id=other_org.id, status="in_progress",
            )
            db.add(cross_task)
            await db.commit()
            await db.refresh(cross_task)
            try:
                await update_task_status(cross_task.id, TaskStatusUpdate(status="done"), background_tasks=BackgroundTasks(), tenant=tm_tenant, db=db)
                raise AssertionError("a Task in a different organization must never be reachable at all")
            except Exception as exc:
                # get_task_or_404 is org-scoped — a cross-tenant id simply
                # doesn't resolve, regardless of the new completion policy.
                assert getattr(exc, "status_code", None) in (403, 404), exc
            await db.execute(delete(Task).where(Task.id == cross_task.id))
            await db.execute(delete(Team).where(Team.id == cross_team.id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == other_org.id))
            await db.execute(delete(User).where(User.id == cross_owner.id))
            await db.commit()

        finally:
            if created_task_ids:
                await db.execute(delete(Task).where(Task.id.in_(created_task_ids)))
            await db.execute(delete(ProjectTeam).where(ProjectTeam.project_id == clarvs.id))
            await db.execute(delete(ProjectMembership).where(ProjectMembership.project_id == clarvs.id))
            await db.execute(delete(Project).where(Project.id.in_([clarvs.id, unrelated_project.id])))
            await db.execute(delete(TeamMembership).where(TeamMembership.team_id.in_([technology.id, marketing.id])))
            await db.execute(delete(Team).where(Team.id.in_([technology.id, marketing.id])))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id.in_([org.id, other_org.id])))
            await db.execute(delete(User).where(User.id.in_([owner.id, tm.id, other_tm.id, pm.id, member.id])))
            await db.commit()

    await engine.dispose()


def test_task_completion_policy():
    asyncio.run(_run())
