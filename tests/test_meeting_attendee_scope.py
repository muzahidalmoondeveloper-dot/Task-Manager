"""Meeting Attendees scope regression test (hardening pass).

Root cause: CreateMeetingPage (and CreateMeetingModal / the live meeting
"add attendee" picker) called the org-wide GET /users (Owner/Admin only).
A plain Project Manager's request 403'd and was silently swallowed by the
frontend's `.catch()`, leaving "Select Attendee to Add" empty.

Fix: a scoped GET /meetings/eligible-attendees endpoint
(`app.api.routes.meetings.list_eligible_attendees` /
`_eligible_attendee_ids`). Authoritative rule (hardening pass): a Client
is NEVER an eligible Meeting attendee — for ANY role, even with a
TeamMembership row — decided only by the authoritative
`OrganizationMembership.role` (never the legacy `User.role`). Inactive
users are excluded the same way. Only a plain Project Manager gets a
narrower, Project/Team-scoped set (every other actor who can reach this at
all keeps its existing org-wide-within-the-organization reach, minus
Clients/inactive users). `create_meeting`/`update_meeting` independently
re-validate `participant_ids` server-side (`_validate_attendee_ids`) — the
scoped dropdown is a convenience, never the security boundary.

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from fastapi import HTTPException
from sqlalchemy import delete

from app.api.routes.meetings import (
    _eligible_attendee_ids,
    create_meeting,
    list_eligible_attendees,
    update_meeting,
)
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import CLIENT, PROJECT_MANAGER, TEAM_MANAGER
from app.core.tenant import TenantContext
from app.models.meeting import Meeting, MeetingParticipant
from app.models.organization import Organization, OrganizationMembership
from app.models.project import Project, ProjectMembership, ProjectTeam
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.schemas.meeting import MeetingCreate, MeetingUpdate


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="AttScope Owner", email=f"attscope.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        pm = User(full_name="AttScope PM", email=f"attscope.pm.{suffix}@test.invalid", hashed_password="x", role=PROJECT_MANAGER)
        tm = User(full_name="AttScope TM", email=f"attscope.tm.{suffix}@test.invalid", hashed_password="x", role=TEAM_MANAGER)
        team_member = User(full_name="AttScope Member", email=f"attscope.member.{suffix}@test.invalid", hashed_password="x", role="team_member")
        client_a = User(full_name="AttScope Client", email=f"attscope.client.{suffix}@test.invalid", hashed_password="x", role=CLIENT)
        client_with_team = User(full_name="AttScope Client+Team", email=f"attscope.clientteam.{suffix}@test.invalid", hashed_password="x", role=CLIENT)
        inactive_member = User(full_name="AttScope Inactive", email=f"attscope.inactive.{suffix}@test.invalid", hashed_password="x", role="team_member")
        unrelated_member = User(full_name="AttScope Unrelated", email=f"attscope.unrelated.{suffix}@test.invalid", hashed_password="x", role="team_member")
        db.add_all([owner, pm, tm, team_member, client_a, client_with_team, inactive_member, unrelated_member])
        await db.flush()

        # A second, wholly separate organization — the cross-tenant probe.
        other_owner = User(full_name="OtherOrg Owner", email=f"attscope.otherowner.{suffix}@test.invalid", hashed_password="x", role="owner")
        other_org_user = User(full_name="OtherOrg User", email=f"attscope.otheruser.{suffix}@test.invalid", hashed_password="x", role="team_member")
        db.add_all([other_owner, other_org_user])
        await db.flush()

        org = Organization(name=f"AttScope Org {suffix}", slug=f"attscope-{suffix}", owner_id=owner.id)
        other_org = Organization(name=f"AttScope OtherOrg {suffix}", slug=f"attscope-other-{suffix}", owner_id=other_owner.id)
        db.add_all([org, other_org])
        await db.flush()

        owner_membership = OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner")
        pm_membership = OrganizationMembership(organization_id=org.id, user_id=pm.id, role=PROJECT_MANAGER)
        tm_membership = OrganizationMembership(organization_id=org.id, user_id=tm.id, role=TEAM_MANAGER)
        inactive_membership = OrganizationMembership(organization_id=org.id, user_id=inactive_member.id, role="team_member", is_active=False)
        memberships = [
            owner_membership,
            pm_membership,
            tm_membership,
            OrganizationMembership(organization_id=org.id, user_id=team_member.id, role="team_member"),
            OrganizationMembership(organization_id=org.id, user_id=client_a.id, role=CLIENT),
            OrganizationMembership(organization_id=org.id, user_id=client_with_team.id, role=CLIENT),
            inactive_membership,
            OrganizationMembership(organization_id=org.id, user_id=unrelated_member.id, role="team_member"),
            OrganizationMembership(organization_id=other_org.id, user_id=other_owner.id, role="owner"),
            OrganizationMembership(organization_id=other_org.id, user_id=other_org_user.id, role="team_member"),
        ]
        db.add_all(memberships)
        await db.flush()

        # project_a: the one the PM actually manages. project_b: a wholly
        # unrelated Project in the SAME org (its own member must never
        # leak into the PM's attendee options).
        project_a = Project(name=f"AttScope Project A {suffix}", organization_id=org.id, created_by_id=owner.id)
        project_b = Project(name=f"AttScope Project B {suffix}", organization_id=org.id, created_by_id=owner.id)
        db.add_all([project_a, project_b])
        await db.flush()

        team_a = Team(name=f"AttScope Team A {suffix}", organization_id=org.id, team_manager_id=tm.id, created_by_id=owner.id)
        db.add(team_a)
        await db.flush()

        db.add_all([
            ProjectMembership(project_id=project_a.id, user_id=pm.id),                # PM manages project_a
            ProjectMembership(project_id=project_a.id, user_id=client_a.id),          # Client granted access via ProjectMembership only
            ProjectMembership(project_id=project_b.id, user_id=unrelated_member.id),  # unrelated project's own member
            ProjectTeam(project_id=project_a.id, team_id=team_a.id, assigned_by_id=owner.id),
            TeamMembership(team_id=team_a.id, user_id=tm.id),               # manager also carries a membership row
            TeamMembership(team_id=team_a.id, user_id=team_member.id),
            TeamMembership(team_id=team_a.id, user_id=client_with_team.id), # Client WITH a TeamMembership row — must still be excluded
            TeamMembership(team_id=team_a.id, user_id=inactive_member.id), # inactive user WITH a TeamMembership row — must still be excluded
        ])
        await db.commit()

        when = datetime.now(timezone.utc) + timedelta(days=3)
        owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=owner_membership, user=owner, db=db)
        pm_tenant = TenantContext(organization_id=org.id, organization=org, membership=pm_membership, user=pm, db=db)
        tm_tenant = TenantContext(organization_id=org.id, organization=org, membership=tm_membership, user=tm, db=db)

        created_ids = []
        project_c = None
        try:
            # ── 1/3. PM with a managed Project sees valid internal
            # attendees: themself, the attached Team's manager and member. ──
            pm_eligible = await _eligible_attendee_ids(db, pm_tenant)
            assert pm_eligible == {pm.id, tm.id, team_member.id}, f"unexpected PM-scoped set: {pm_eligible}"

            # ── 4. unrelated Project's own member never appears. ──
            assert unrelated_member.id not in pm_eligible

            # ── 5. Client (ProjectMembership only) never appears. ──
            assert client_a.id not in pm_eligible

            # ── 6. Client WITH a TeamMembership row still never appears. ──
            assert client_with_team.id not in pm_eligible

            # ── 7. inactive user (OrganizationMembership.is_active=False),
            # even with a TeamMembership row, is excluded. ──
            assert inactive_member.id not in pm_eligible

            # The scoped listing endpoint returns exactly this same set.
            pm_options = await list_eligible_attendees(db=db, tenant=pm_tenant)
            assert {u.id for u in pm_options} == pm_eligible

            # ── 2. Multiple managed Projects — de-duplicated union; a user
            # connected only to a third, unrelated Project never appears. ──
            project_c = Project(name=f"AttScope Project C {suffix}", organization_id=org.id, created_by_id=owner.id)
            db.add(project_c)
            await db.flush()
            db.add(ProjectMembership(project_id=project_c.id, user_id=pm.id))
            db.add(ProjectMembership(project_id=project_c.id, user_id=team_member.id))  # same user, second Project
            project_c_only_user = User(full_name="AttScope ProjectC Only", email=f"attscope.projectc.{suffix}@test.invalid", hashed_password="x", role="team_member")
            db.add(project_c_only_user)
            await db.flush()
            db.add(OrganizationMembership(organization_id=org.id, user_id=project_c_only_user.id, role="team_member"))
            # NOT added to project_c — deliberately absent from the PM's
            # scope despite being a real, active org member, confirming
            # "only a real member of a managed Project" (not "any active
            # org member at all").
            await db.commit()

            union_eligible = await _eligible_attendee_ids(db, pm_tenant)
            assert union_eligible == {pm.id, tm.id, team_member.id}, "overlapping member across two managed Projects must not duplicate"
            assert project_c_only_user.id not in union_eligible

            # ── 8. cross-tenant user excluded. ──
            assert other_org_user.id not in pm_eligible
            assert other_org_user.id not in union_eligible

            # ── 12. Owner/Admin: org-wide reach preserved, Clients/inactive
            # still excluded (narrower than before, never broader). ──
            owner_eligible = await _eligible_attendee_ids(db, owner_tenant)
            assert owner_eligible == {owner.id, pm.id, tm.id, team_member.id, unrelated_member.id, project_c_only_user.id}
            assert client_a.id not in owner_eligible and client_with_team.id not in owner_eligible
            assert inactive_member.id not in owner_eligible
            assert other_org_user.id not in owner_eligible

            # ── 13. Team Manager: same org-wide-minus-Client/inactive reach
            # (existing Meeting-module behavior — Team Manager already
            # manages every meeting in the org, unaffected by this fix). ──
            tm_eligible = await _eligible_attendee_ids(db, tm_tenant)
            assert tm_eligible == owner_eligible

            # ── 9. Valid Meeting creation with eligible attendees succeeds. ──
            created = await create_meeting(
                MeetingCreate(
                    title=f"AttScope PM Meeting {suffix}",
                    scheduled_at=when,
                    participant_ids=[pm.id, tm.id, team_member.id],
                ),
                db=db, tenant=pm_tenant,
            )
            created_ids.append(created.id)
            assert {p.user_id for p in created.participants} == {pm.id, tm.id, team_member.id}

            # ── 10. Direct API request containing a Client attendee is
            # rejected — never silently dropped. ──
            try:
                await create_meeting(
                    MeetingCreate(
                        title=f"AttScope PM Client Meeting {suffix}",
                        scheduled_at=when,
                        participant_ids=[pm.id, client_a.id],
                    ),
                    db=db, tenant=pm_tenant,
                )
                raise AssertionError("a Client attendee must be rejected, not silently accepted")
            except HTTPException as exc:
                assert exc.status_code == 400

            # A Client is rejected for Owner/Admin too — this is a
            # universal Meeting-module rule, not a PM-only narrowing.
            try:
                await create_meeting(
                    MeetingCreate(
                        title=f"AttScope Owner Client Meeting {suffix}",
                        scheduled_at=when,
                        participant_ids=[owner.id, client_a.id],
                    ),
                    db=db, tenant=owner_tenant,
                )
                raise AssertionError("a Client attendee must be rejected for Owner/Admin too")
            except HTTPException as exc:
                assert exc.status_code == 400

            # ── 11. Direct API request containing an unrelated Project's
            # user is rejected for the PM. ──
            try:
                await create_meeting(
                    MeetingCreate(
                        title=f"AttScope PM Unrelated Meeting {suffix}",
                        scheduled_at=when,
                        participant_ids=[pm.id, unrelated_member.id],
                    ),
                    db=db, tenant=pm_tenant,
                )
                raise AssertionError("an unrelated Project's user must be rejected, not silently accepted")
            except HTTPException as exc:
                assert exc.status_code == 400

            # Cross-tenant id is rejected outright, for the PM and for
            # Owner/Admin alike.
            try:
                await create_meeting(
                    MeetingCreate(
                        title=f"AttScope PM CrossTenant {suffix}",
                        scheduled_at=when,
                        participant_ids=[pm.id, other_org_user.id],
                    ),
                    db=db, tenant=pm_tenant,
                )
                raise AssertionError("a cross-tenant attendee must be rejected for a PM")
            except HTTPException as exc:
                assert exc.status_code == 400

            try:
                await create_meeting(
                    MeetingCreate(
                        title=f"AttScope Owner CrossTenant {suffix}",
                        scheduled_at=when,
                        participant_ids=[owner.id, other_org_user.id],
                    ),
                    db=db, tenant=owner_tenant,
                )
                raise AssertionError("a cross-tenant attendee must be rejected even for Owner/Admin")
            except HTTPException as exc:
                assert exc.status_code == 400

            # update_meeting: a PM may still add ONE more scoped attendee to
            # a meeting an Owner created that already includes a participant
            # outside the PM's own Project scope (added earlier by someone
            # else with a wider, legitimate reach) — the pre-existing
            # attendee must never block the new, in-scope addition.
            owner_created = await create_meeting(
                MeetingCreate(
                    title=f"AttScope Owner Meeting {suffix}",
                    scheduled_at=when,
                    participant_ids=[owner.id, unrelated_member.id],
                ),
                db=db, tenant=owner_tenant,
            )
            created_ids.append(owner_created.id)

            updated = await update_meeting(
                owner_created.id,
                MeetingUpdate(participant_ids=[owner.id, unrelated_member.id, team_member.id]),
                db=db, tenant=pm_tenant,
            )
            assert {p.user_id for p in updated.participants} == {owner.id, unrelated_member.id, team_member.id}

            # But the PM adding a genuinely NEW Client is still rejected,
            # even via update_meeting.
            try:
                await update_meeting(
                    owner_created.id,
                    MeetingUpdate(participant_ids=[owner.id, unrelated_member.id, team_member.id, client_a.id]),
                    db=db, tenant=pm_tenant,
                )
                raise AssertionError("a newly-added Client must be rejected on update too")
            except HTTPException as exc:
                assert exc.status_code == 400

            print("test_meeting_attendee_scope: PASSED")
        finally:
            for meeting_id in created_ids:
                await db.execute(delete(MeetingParticipant).where(MeetingParticipant.meeting_id == meeting_id))
                await db.execute(delete(Meeting).where(Meeting.id == meeting_id))
            project_ids = [project_a.id, project_b.id]
            if project_c is not None:
                project_ids.append(project_c.id)
            await db.execute(delete(ProjectMembership).where(ProjectMembership.project_id.in_(project_ids)))
            await db.execute(delete(ProjectTeam).where(ProjectTeam.project_id == project_a.id))
            await db.execute(delete(TeamMembership).where(TeamMembership.team_id == team_a.id))
            await db.execute(delete(Team).where(Team.id == team_a.id))
            await db.execute(delete(Project).where(Project.id.in_(project_ids)))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_([org.id, other_org.id])))
            await db.execute(delete(Organization).where(Organization.id.in_([org.id, other_org.id])))
            user_ids = [
                owner.id, pm.id, tm.id, team_member.id, client_a.id, client_with_team.id,
                inactive_member.id, unrelated_member.id, other_owner.id, other_org_user.id,
            ]
            try:
                user_ids.append(project_c_only_user.id)
            except NameError:
                pass
            await db.execute(delete(User).where(User.id.in_(user_ids)))
            await db.commit()

    await engine.dispose()


def test_meeting_attendee_scope():
    asyncio.run(_scenario())
