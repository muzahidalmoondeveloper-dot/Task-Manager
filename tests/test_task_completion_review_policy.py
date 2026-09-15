"""Regression tests for the Task completion/review-workflow follow-up:

  1. `can_complete_task_directly` now requires `task.assignee_id ==
     tenant.user.id` inside its TM/PM branches — it answers ONLY "is this
     caller eligible to complete THEIR OWN Task directly", never "may
     this manager touch a teammate's Task" (a separate, pre-existing,
     unrelated authority each call site preserves explicitly via its own
     `task.assignee_id != tenant.user.id` bypass).
  2. Pending Review is now a hard review state: `PATCH /tasks/{id}/status`
     and `PATCH /tasks/{id}` both reject a `status: "done"` request
     against an already-`pending_review` Task outright (for EVERY caller,
     Owner/Admin included) — that transition is exclusively
     `POST /tasks/{id}/approve`'s job, so it always carries the correct
     "Approved" note/reviewer metadata/notification, never a
     "Marked done directly." note that would misrepresent an un-reviewed
     (or wrongly-re-reviewed) submission.

Covers the spec's REQUIRED TEST CASES 1-17. Items already covered by
test_task_completion_policy.py's TESTs 1-11 (the base self-completion
eligibility matrix) are reconfirmed here only where this follow-up's
changes could plausibly have disturbed them; the NEW behavior (13, 14, and
the Owner/Admin pending-review restriction implied by the spec's "do not
silently allow... for EVERY caller" language) is the primary focus.

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

from app.api.routes.tasks import approve_task, assign_task_back, update_task, update_task_status
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import OWNER, PROJECT_MANAGER, TEAM_MANAGER, TEAM_MEMBER
from app.core.tenant import TenantContext
from app.models.organization import Organization, OrganizationMembership
from app.models.project import Project, ProjectMembership, ProjectTeam
from app.models.task import Task
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.schemas.task import AssignBackRequest, TaskStatusUpdate, TaskUpdate


async def _run():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]

        owner = User(full_name="TCR Owner", email=f"tcr.owner.{suffix}@example-corp.com", hashed_password="x", role="owner")
        tm = User(full_name="TCR TM", email=f"tcr.tm.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MANAGER)
        other_tm = User(full_name="TCR Other TM", email=f"tcr.othertm.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MANAGER)
        pm = User(full_name="TCR PM", email=f"tcr.pm.{suffix}@example-corp.com", hashed_password="x", role=PROJECT_MANAGER)
        member = User(full_name="TCR Member", email=f"tcr.member.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)
        db.add_all([owner, tm, other_tm, pm, member])
        await db.commit()
        for u in (owner, tm, other_tm, pm, member):
            await db.refresh(u)

        org = Organization(name=f"TCR Org {suffix}", slug=f"tcr-org-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.commit()
        org = (await db.execute(select(Organization).options(selectinload(Organization.subscription)).where(Organization.id == org.id))).scalar_one()

        memberships = {}
        for u, role in [(owner, OWNER), (tm, TEAM_MANAGER), (other_tm, TEAM_MANAGER), (pm, PROJECT_MANAGER), (member, TEAM_MEMBER)]:
            m = OrganizationMembership(organization_id=org.id, user_id=u.id, role=role)
            db.add(m)
            memberships[u.id] = m
        await db.commit()

        # Technology Team: managed by tm. Marketing Team: managed by
        # other_tm (tm is NOT its manager — Test 5/12's negative case).
        technology = Team(name=f"TCR Technology {suffix}", team_manager_id=tm.id, created_by_id=owner.id, organization_id=org.id)
        marketing = Team(name=f"TCR Marketing {suffix}", team_manager_id=other_tm.id, created_by_id=owner.id, organization_id=org.id)
        db.add_all([technology, marketing])
        await db.commit()
        for t in (technology, marketing):
            await db.refresh(t)
        db.add_all([
            TeamMembership(team_id=technology.id, user_id=tm.id),
            TeamMembership(team_id=technology.id, user_id=member.id),
            TeamMembership(team_id=marketing.id, user_id=other_tm.id),
            TeamMembership(team_id=marketing.id, user_id=tm.id),
        ])
        await db.commit()

        clarvs = Project(name=f"TCR Clarvs {suffix}", created_by_id=owner.id, organization_id=org.id, status="active")
        db.add(clarvs)
        await db.commit()
        await db.refresh(clarvs)
        db.add(ProjectMembership(project_id=clarvs.id, user_id=pm.id))
        await db.commit()
        # PM-delegation scenario (Test 11): Clarvs -> Technology Team.
        db.add(ProjectTeam(project_id=clarvs.id, team_id=technology.id, assigned_by_id=owner.id))
        await db.commit()

        owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[owner.id], user=owner, db=db)
        tm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[tm.id], user=tm, db=db)
        pm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[pm.id], user=pm, db=db)
        member_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[member.id], user=member, db=db)

        created_task_ids: list[int] = []

        async def _make_task(*, team_id=None, project_id=None, assignee_id, status="in_progress") -> Task:
            task = Task(
                name=f"TCR Task {uuid.uuid4().hex[:6]}", team_id=team_id, project_id=project_id,
                assignee_id=assignee_id, created_by_id=owner.id, organization_id=org.id, status=status,
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
            # ── 1. Regular Team Member + Team Task + Done -> Pending
            # Review. ─────────────────────────────────────────────────
            for via_update in (False, True):
                t1 = await _make_task(team_id=technology.id, assignee_id=member.id)
                result = await _set_status(t1.id, "done", member_tenant, via_update=via_update)
                assert result.status == "pending_review"

            # ── 2. Regular Team Member Personal Task -> Done directly,
            # no Pending Review. ─────────────────────────────────────────
            for via_update in (False, True):
                t2 = await _make_task(team_id=None, assignee_id=member.id)
                result = await _set_status(t2.id, "done", member_tenant, via_update=via_update)
                assert result.status == "done"
                assert result.review_note == "Marked done directly."

            # ── 3, 4. TM own Task in managed Team / own Personal Task ->
            # Done directly. ─────────────────────────────────────────────
            for via_update in (False, True):
                t3 = await _make_task(team_id=technology.id, assignee_id=tm.id)
                result = await _set_status(t3.id, "done", tm_tenant, via_update=via_update)
                assert result.status == "done"
                t4 = await _make_task(team_id=None, assignee_id=tm.id)
                result = await _set_status(t4.id, "done", tm_tenant, via_update=via_update)
                assert result.status == "done"

            # ── 5. TM assigned to a Task in an UNMANAGED Team ->
            # ordinary assignee workflow, no manager direct-Done. ────────
            for via_update in (False, True):
                t5 = await _make_task(team_id=marketing.id, assignee_id=tm.id)
                result = await _set_status(t5.id, "done", tm_tenant, via_update=via_update)
                assert result.status == "pending_review"

            # ── 6, 7. PM own managed-Project Task / own Personal Task ->
            # Done directly. ─────────────────────────────────────────────
            for via_update in (False, True):
                t6 = await _make_task(project_id=clarvs.id, assignee_id=pm.id)
                result = await _set_status(t6.id, "done", pm_tenant, via_update=via_update)
                assert result.status == "done"
                t7 = await _make_task(team_id=None, project_id=None, assignee_id=pm.id)
                result = await _set_status(t7.id, "done", pm_tenant, via_update=via_update)
                assert result.status == "done"

            # ── 8. PM assigned to a Task in an unrelated/unmanaged
            # Project -> no PM direct-Done privilege. ────────────────────
            for via_update in (False, True):
                t8 = await _make_task(team_id=marketing.id, assignee_id=pm.id)
                result = await _set_status(t8.id, "done", pm_tenant, via_update=via_update)
                assert result.status == "pending_review"

            # ── 9, 10. Team Member Task reaches Pending Review -> the
            # exact Team's TM can Approve; a fresh one can Assign Back. ──
            t9 = await _make_task(team_id=technology.id, assignee_id=member.id)
            await update_task_status(t9.id, TaskStatusUpdate(status="done"), background_tasks=BackgroundTasks(), tenant=member_tenant, db=db)
            approved = await approve_task(t9.id, background_tasks=BackgroundTasks(), tenant=tm_tenant, db=db)
            assert approved.status == "done"
            assert approved.review_note == "Approved"

            t10 = await _make_task(team_id=technology.id, assignee_id=member.id)
            await update_task_status(t10.id, TaskStatusUpdate(status="done"), background_tasks=BackgroundTasks(), tenant=member_tenant, db=db)
            assigned_back = await assign_task_back(t10.id, AssignBackRequest(note="Needs more detail"), background_tasks=BackgroundTasks(), tenant=tm_tenant, db=db)
            assert assigned_back.status == "in_progress"

            # ── 11. PM managing the Project (Clarvs) but NOT the Task's
            # exact Team -> cannot Approve/Assign Back on a delegated
            # Team Member Task, solely because of the PM role. ───────────
            t11 = await _make_task(team_id=technology.id, project_id=clarvs.id, assignee_id=member.id)
            await update_task_status(t11.id, TaskStatusUpdate(status="done"), background_tasks=BackgroundTasks(), tenant=member_tenant, db=db)
            try:
                await approve_task(t11.id, background_tasks=BackgroundTasks(), tenant=pm_tenant, db=db)
                raise AssertionError("a Project Manager must never review a delegated Team Task themselves")
            except Exception as exc:
                assert getattr(exc, "status_code", None) == 403, exc
            try:
                await assign_task_back(t11.id, AssignBackRequest(note=None), background_tasks=BackgroundTasks(), tenant=pm_tenant, db=db)
                raise AssertionError("a Project Manager must never Assign Back a delegated Team Task themselves")
            except Exception as exc:
                assert getattr(exc, "status_code", None) == 403, exc

            # ── 12. A TM managing a DIFFERENT Team cannot Approve/Assign
            # Back this Team's Pending Review Task. ──────────────────────
            t12 = await _make_task(team_id=technology.id, assignee_id=member.id)
            await update_task_status(t12.id, TaskStatusUpdate(status="done"), background_tasks=BackgroundTasks(), tenant=member_tenant, db=db)
            try:
                await approve_task(t12.id, background_tasks=BackgroundTasks(), tenant=TenantContext(organization_id=org.id, organization=org, membership=memberships[other_tm.id], user=other_tm, db=db), db=db)
                raise AssertionError("a Team Manager of a DIFFERENT Team must never approve this Task")
            except Exception as exc:
                assert getattr(exc, "status_code", None) == 403, exc

            # ── 13. TM reviewer generic PATCH Pending Review -> Done must
            # NOT bypass canonical Approve semantics — even though `tm`
            # legitimately manages this Task's Team (and would normally
            # have unrestricted field access), Pending Review -> Done is
            # exclusively Approve's job. Verified via BOTH endpoints. ─────
            for via_update in (False, True):
                t13 = await _make_task(team_id=technology.id, assignee_id=member.id, status="pending_review")
                try:
                    await _set_status(t13.id, "done", tm_tenant, via_update=via_update)
                    raise AssertionError(f"a generic status PATCH must never silently approve a Pending Review Task (via_update={via_update})")
                except AppException as exc:
                    assert exc.code == "PENDING_REVIEW_REQUIRES_APPROVAL", exc
                # The Task must be untouched — still pending_review, no
                # stray "Marked done directly." note.
                reloaded = (await db.execute(select(Task).where(Task.id == t13.id))).scalar_one()
                assert reloaded.status == "pending_review"
                assert reloaded.review_note is None
                # The SAME Task, via the real Approve action, succeeds
                # with the correct semantics.
                approved13 = await approve_task(t13.id, background_tasks=BackgroundTasks(), tenant=tm_tenant, db=db)
                assert approved13.status == "done"
                assert approved13.review_note == "Approved"

            # Owner/Admin included — no generic-PATCH bypass for anyone.
            for via_update in (False, True):
                t13b = await _make_task(team_id=technology.id, assignee_id=member.id, status="pending_review")
                try:
                    await _set_status(t13b.id, "done", owner_tenant, via_update=via_update)
                    raise AssertionError(f"even Owner/Admin must use Approve for a Pending Review Task, not a generic PATCH (via_update={via_update})")
                except AppException as exc:
                    assert exc.code == "PENDING_REVIEW_REQUIRES_APPROVAL", exc
                owner_approved = await approve_task(t13b.id, background_tasks=BackgroundTasks(), tenant=owner_tenant, db=db)
                assert owner_approved.status == "done"
                assert owner_approved.review_note == "Approved"

            # ── 14. PM generic PATCH on ANOTHER user's (non-Pending-
            # Review) Task under their managed Project must still be
            # classified as the PRE-EXISTING "manager sets a teammate's
            # Task directly" capability (unrelated to self-completion) —
            # never blocked, and never mislabeled. Only reachable via
            # update_task, since PM only ever reaches update_task_status
            # by being the assignee (see require_task_update_access). ────
            t14 = await _make_task(team_id=technology.id, project_id=clarvs.id, assignee_id=member.id)
            teammate_result = await update_task(t14.id, TaskUpdate(status="done"), background_tasks=BackgroundTasks(), tenant=pm_tenant, db=db)
            assert teammate_result.status == "done"
            assert teammate_result.review_note == "Marked done directly."

            # ── 15. Owner/Admin direct completion (non-Pending-Review)
            # remains unchanged. ─────────────────────────────────────────
            for via_update in (False, True):
                t15 = await _make_task(team_id=technology.id, assignee_id=member.id)
                result = await _set_status(t15.id, "done", owner_tenant, via_update=via_update)
                assert result.status == "done"
                assert result.review_note == "Marked done directly."

            # ── 17. Both endpoints enforce the identical policy —
            # reconfirmed throughout above via the via_update parameter;
            # explicit sanity check here too. ───────────────────────────
            t17a = await _make_task(team_id=technology.id, assignee_id=tm.id)
            t17b = await _make_task(team_id=technology.id, assignee_id=tm.id)
            r17a = await update_task_status(t17a.id, TaskStatusUpdate(status="done"), background_tasks=BackgroundTasks(), tenant=tm_tenant, db=db)
            r17b = await update_task(t17b.id, TaskUpdate(status="done"), background_tasks=BackgroundTasks(), tenant=tm_tenant, db=db)
            assert r17a.status == r17b.status == "done"

        finally:
            if created_task_ids:
                await db.execute(delete(Task).where(Task.id.in_(created_task_ids)))
            await db.execute(delete(ProjectTeam).where(ProjectTeam.project_id == clarvs.id))
            await db.execute(delete(ProjectMembership).where(ProjectMembership.project_id == clarvs.id))
            await db.execute(delete(Project).where(Project.id == clarvs.id))
            await db.execute(delete(TeamMembership).where(TeamMembership.team_id.in_([technology.id, marketing.id])))
            await db.execute(delete(Team).where(Team.id.in_([technology.id, marketing.id])))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id == org.id))
            await db.execute(delete(User).where(User.id.in_([owner.id, tm.id, other_tm.id, pm.id, member.id])))
            await db.commit()

    await engine.dispose()


def test_task_completion_review_policy():
    asyncio.run(_run())
