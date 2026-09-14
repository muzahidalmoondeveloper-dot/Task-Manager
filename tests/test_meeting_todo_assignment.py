"""Meeting To-Do (POST /meetings/{id}/tasks) assignment-permission regression
test — Meeting permission-scoping follow-up.

Root cause: `create_meeting_task` accepted `assignee_id` with NO validation
at all — any meeting participant could, via a direct/manipulated request,
create a real Task assigned to anyone, completely bypassing the existing
Task delegation model (`app.core.task_assignment.validate_task_assignee`,
and the Project-Manager-cannot-directly-assign rule `create_task()` already
enforces). Fixed by reusing those same authoritative helpers rather than
inventing a second, weaker Task permission system for Meetings.

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from fastapi import HTTPException

from sqlalchemy import delete

from app.api.routes.meetings import create_meeting, create_meeting_task
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import CLIENT, PROJECT_MANAGER, TEAM_MANAGER
from app.core.tenant import TenantContext
from app.models.meeting import Meeting, MeetingParticipant, MeetingTask
from app.models.organization import Organization, OrganizationMembership
from app.models.task import Task
from app.models.user import User
from app.schemas.meeting import MeetingCreate, MeetingCreateTask


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="MtgTodo Owner", email=f"mtgtodo.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        pm = User(full_name="MtgTodo PM", email=f"mtgtodo.pm.{suffix}@test.invalid", hashed_password="x", role=PROJECT_MANAGER)
        tm = User(full_name="MtgTodo TM", email=f"mtgtodo.tm.{suffix}@test.invalid", hashed_password="x", role=TEAM_MANAGER)
        team_member = User(full_name="MtgTodo Member", email=f"mtgtodo.member.{suffix}@test.invalid", hashed_password="x", role="team_member")
        plain_member = User(full_name="MtgTodo Plain", email=f"mtgtodo.plain.{suffix}@test.invalid", hashed_password="x", role="team_member")
        client_user = User(full_name="MtgTodo Client", email=f"mtgtodo.client.{suffix}@test.invalid", hashed_password="x", role=CLIENT)
        other_org_owner = User(full_name="MtgTodo OtherOwner", email=f"mtgtodo.otherowner.{suffix}@test.invalid", hashed_password="x", role="owner")
        other_org_user = User(full_name="MtgTodo OtherUser", email=f"mtgtodo.otheruser.{suffix}@test.invalid", hashed_password="x", role="team_member")
        db.add_all([owner, pm, tm, team_member, plain_member, client_user, other_org_owner, other_org_user])
        await db.flush()

        org = Organization(name=f"MtgTodo Org {suffix}", slug=f"mtgtodo-{suffix}", owner_id=owner.id)
        other_org = Organization(name=f"MtgTodo OtherOrg {suffix}", slug=f"mtgtodo-other-{suffix}", owner_id=other_org_owner.id)
        db.add_all([org, other_org])
        await db.flush()

        owner_membership = OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner")
        pm_membership = OrganizationMembership(organization_id=org.id, user_id=pm.id, role=PROJECT_MANAGER)
        tm_membership = OrganizationMembership(organization_id=org.id, user_id=tm.id, role=TEAM_MANAGER)
        plain_membership = OrganizationMembership(organization_id=org.id, user_id=plain_member.id, role="team_member")
        db.add_all([
            owner_membership, pm_membership, tm_membership, plain_membership,
            OrganizationMembership(organization_id=org.id, user_id=team_member.id, role="team_member"),
            OrganizationMembership(organization_id=org.id, user_id=client_user.id, role=CLIENT),
            OrganizationMembership(organization_id=other_org.id, user_id=other_org_owner.id, role="owner"),
            OrganizationMembership(organization_id=other_org.id, user_id=other_org_user.id, role="team_member"),
        ])
        await db.commit()

        when = datetime.now(timezone.utc) + timedelta(days=3)
        owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=owner_membership, user=owner, db=db)
        pm_tenant = TenantContext(organization_id=org.id, organization=org, membership=pm_membership, user=pm, db=db)
        tm_tenant = TenantContext(organization_id=org.id, organization=org, membership=tm_membership, user=tm, db=db)
        plain_tenant = TenantContext(organization_id=org.id, organization=org, membership=plain_membership, user=plain_member, db=db)

        meeting_ids = []
        task_ids = []
        try:
            meeting = await create_meeting(
                MeetingCreate(title=f"MtgTodo Meeting {suffix}", scheduled_at=when, participant_ids=[owner.id, plain_member.id]),
                db=db, tenant=owner_tenant,
            )
            meeting_ids.append(meeting.id)

            # ── 1. A plain Team Member (not manager/PM) may not add a
            # meeting to-do at all — even as a genuine participant of this
            # exact meeting (so this exercises create_meeting_task's own
            # new permission gate, not just _get_meeting's separate
            # "can you even see this meeting" check). Matches the only UI
            # entry points that ever exposed this action. ──
            try:
                await create_meeting_task(meeting.id, MeetingCreateTask(name="Should never exist"), db=db, tenant=plain_tenant)
                raise AssertionError("a plain Team Member must not be able to add a meeting to-do")
            except HTTPException as exc:
                assert exc.status_code == 403

            # ── 2. Owner/Admin: unassigned to-do succeeds (existing,
            # unaffected behavior — matches the only payload shape the
            # frontend actually sends today). ──
            mt = await create_meeting_task(meeting.id, MeetingCreateTask(name="Unassigned todo"), db=db, tenant=owner_tenant)
            task_ids.append(mt.task_id)
            assert mt.task_id is not None

            # ── 3. Owner/Admin: assigning a real, active org member
            # succeeds. ──
            mt2 = await create_meeting_task(meeting.id, MeetingCreateTask(name="Assigned by owner", assignee_id=team_member.id), db=db, tenant=owner_tenant)
            task_ids.append(mt2.task_id)
            created_task = (await db.execute(Task.__table__.select().where(Task.id == mt2.task_id))).first()
            assert created_task.assignee_id == team_member.id

            # ── 4. A Client may never be an assignee — rejected even for
            # Owner/Admin. ──
            try:
                await create_meeting_task(meeting.id, MeetingCreateTask(name="Bad", assignee_id=client_user.id), db=db, tenant=owner_tenant)
                raise AssertionError("a Client must never be assignable via a meeting to-do")
            except AppException as exc:
                assert exc.status_code == 400

            # ── 5. Cross-tenant assignee id is rejected outright. ──
            try:
                await create_meeting_task(meeting.id, MeetingCreateTask(name="Bad", assignee_id=other_org_user.id), db=db, tenant=owner_tenant)
                raise AssertionError("a cross-tenant assignee must be rejected")
            except AppException as exc:
                assert exc.status_code == 400

            # ── 6. A plain Project Manager may create their OWN, self-
            # assigned to-do ("My Task" delegation-model equivalent). ──
            mt3 = await create_meeting_task(meeting.id, MeetingCreateTask(name="PM self todo", assignee_id=pm.id), db=db, tenant=pm_tenant)
            task_ids.append(mt3.task_id)

            # ── 7. A plain Project Manager may NEVER directly assign an
            # individual Team Member — rejected, not silently stripped. ──
            try:
                await create_meeting_task(meeting.id, MeetingCreateTask(name="PM assigns member", assignee_id=team_member.id), db=db, tenant=pm_tenant)
                raise AssertionError("a plain Project Manager must not be able to directly assign a Team Member")
            except AppException as exc:
                assert exc.code == "PROJECT_MANAGER_CANNOT_ASSIGN_MEMBER"

            # ── 8. Team Manager: assigning a valid org member succeeds
            # (existing, unaffected behavior). ──
            mt4 = await create_meeting_task(meeting.id, MeetingCreateTask(name="TM assigns member", assignee_id=team_member.id), db=db, tenant=tm_tenant)
            task_ids.append(mt4.task_id)

            print("test_meeting_todo_assignment: PASSED")
        finally:
            if task_ids:
                await db.execute(delete(MeetingTask).where(MeetingTask.task_id.in_(task_ids)))
                await db.execute(delete(Task).where(Task.id.in_(task_ids)))
            for meeting_id in meeting_ids:
                await db.execute(delete(MeetingParticipant).where(MeetingParticipant.meeting_id == meeting_id))
                await db.execute(delete(Meeting).where(Meeting.id == meeting_id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_([org.id, other_org.id])))
            await db.execute(delete(Organization).where(Organization.id.in_([org.id, other_org.id])))
            await db.execute(delete(User).where(User.id.in_([
                owner.id, pm.id, tm.id, team_member.id, plain_member.id, client_user.id,
                other_org_owner.id, other_org_user.id,
            ])))
            await db.commit()

    await engine.dispose()


def test_meeting_todo_assignment():
    asyncio.run(_scenario())
