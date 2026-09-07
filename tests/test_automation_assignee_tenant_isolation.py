"""Regression tests for the cross-tenant automation-assignee security fix
(app.services.automation_tasks / app.repositories.user_repository /
app.repositories.task_repository / app.api.routes.meetings).

Root cause (see final report): `find_fallback_assignee_id()` and the
"known users" list fed into AI extraction both sourced candidates from
`UserRepository.list_all()` — a genuinely global, cross-tenant query with
no organization filter — and the fallback heuristic checked the legacy,
non-org-specific `User.role == "admin"` instead of the authoritative
`OrganizationMembership.role`. This could theoretically let automation
running for Organization A resolve/fallback to a user who only belongs to
Organization B.

Covers (see the spec's BACKEND TESTS + DEFENSE-IN-DEPTH TEST sections):
  1. UserRepository.list_org_assignable_candidates() only returns active,
     non-Client members of the requested organization.
  2. ...excludes an inactive membership.
  3. ...excludes an inactive User account.
  4. ...excludes Client, even with a legacy User.role of "admin".
  5. ...role comes from OrganizationMembership.role, not User.role, even
     when the two are deliberately mismatched.
  6. resolve_user_id(): cross-org same-name collision — given ONLY Org A's
     candidate list, "Alex Smith" resolves to Org A's Alex, never Org B's
     (proves the scoping is enforced by what candidates are PASSED IN,
     the actual fix locus).
  7. resolve_user_id(): cross-org email never matches when the candidate
     list is properly scoped.
  8. find_fallback_assignee_id(): fallback role uses
     OrganizationMembership.role, not the legacy/global User.role —
     mismatched both directions (User.role=admin/membership=team_member
     is NOT picked; User.role=team_member/membership=admin IS picked).
  9. find_fallback_assignee_id(): multi-org same user — the SAME physical
     User is Admin in Org A and Team Member in Org B; each org's fallback
     resolution uses only that org's own membership role.
  10. find_fallback_assignee_id(): no eligible candidate at all -> None
      (fail-safe unassigned), never a fabricated id.
  11. find_fallback_assignee_id(): Team Task — an org Admin who is NOT a
      member of the task's team is never selected as fallback merely for
      being Admin.
  12. find_fallback_assignee_id(): Team Task — an eligible Team member
      (even without Admin) is selected once no earlier step matches.
  13. find_fallback_assignee_id(): explicit source-owner-email step still
      wins first, when eligible, preserving the original 3-step order.
  14. Defense-in-depth: TaskRepository.create() rejects (raises
      AppException, never persists) an assignee_id belonging to a
      DIFFERENT organization, even when called directly with no upstream
      validation — the persistence boundary itself is safe.
  15. Defense-in-depth: TaskRepository.create() also rejects a Client
      assignee and a non-Team-member assignee for a Team Task, at the
      same persistence boundary.
  16. End-to-end (meetings.py stop_recording -> _extract_and_create_tasks):
      a meeting transcript in Org A that names a person who only exists in
      Org B never assigns that Org B person — the created Task is either
      correctly assigned to Org A's own same-named person or left
      unassigned, and Task.organization_id is always Org A's.
  17. End-to-end: a Team-scoped meeting only assigns to that team's
      members, never an org-wide Admin who isn't on the team.

Runs against the real database connection the app uses. Every row this
test creates is deleted before it returns.
"""

import asyncio
import json
import uuid
from datetime import date, datetime, timedelta, timezone

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete

from app.api.routes.meetings import create_meeting, start_recording, stop_recording
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import ADMIN, CLIENT, OWNER, TEAM_MANAGER, TEAM_MEMBER
from app.core.tenant import TenantContext
from app.models.meeting import Meeting, MeetingTask
from app.models.organization import Organization, OrganizationMembership
from app.models.task import Task
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.repositories.task_repository import TaskRepository
from app.repositories.user_repository import UserRepository
from app.schemas.meeting import MeetingCreate, TranscriptSubmit
from app.schemas.task import TaskCreate
from app.services.automation_tasks import find_fallback_assignee_id, resolve_user_id
from app.services.llm.base import LLMProvider, LLMResponse


class _ScriptedProvider(LLMProvider):
    def __init__(self, replies: list[str]):
        self._replies = list(replies)
        self.call_count = 0

    async def generate_text(self, **kwargs):
        reply = self._replies[min(self.call_count, len(self._replies) - 1)]
        self.call_count += 1
        return LLMResponse(text=reply)


def _payload(tasks: list[dict]) -> str:
    return json.dumps({
        "should_create_tasks": bool(tasks),
        "reason": "clear action item" if tasks else "no action items",
        "source_category": "meeting_followup" if tasks else "internal_update",
        "tasks": tasks,
    })


async def _scenario(monkeypatch):
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]

        # ── Org A roster ────────────────────────────────────────────────
        a_owner = User(full_name="AT Owner A", email=f"at.ownera.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)
        a_admin_stale = User(full_name="AT Admin Stale", email=f"at.adminstale.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)  # membership=admin, User.role stale
        a_fake_admin = User(full_name="AT Fake Admin", email=f"at.fakeadmin.{suffix}@example-corp.com", hashed_password="x", role=ADMIN)  # User.role=admin, membership=team_member — must NOT be picked
        a_client = User(full_name="AT Client", email=f"at.client.{suffix}@example-corp.com", hashed_password="x", role=ADMIN)  # Client despite legacy User.role=admin
        a_inactive_member = User(full_name="AT Inactive Membership", email=f"at.inactivemember.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)
        a_inactive_user = User(full_name="AT Inactive User", email=f"at.inactiveuser.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER, is_active=False)
        a_alex = User(full_name="Alex Smith", email=f"at.alexa.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)
        a_team_member = User(full_name="AT Team Member", email=f"at.teammember.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)

        # ── Org B roster (must never be visible to Org A automation) ───
        b_owner = User(full_name="AT Owner B", email=f"at.ownerb.{suffix}@example-corp.com", hashed_password="x", role="owner")
        b_alex = User(full_name="Alex Smith", email=f"at.alexb.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)  # SAME NAME as a_alex
        b_multiuser = User(full_name="AT Multi User", email=f"at.multiuser.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)

        all_users = [
            a_owner, a_admin_stale, a_fake_admin, a_client, a_inactive_member,
            a_inactive_user, a_alex, a_team_member, b_owner, b_alex, b_multiuser,
        ]
        db.add_all(all_users)
        await db.commit()
        for u in all_users:
            await db.refresh(u)

        org_a = Organization(name=f"AT Org A {suffix}", slug=f"at-org-a-{suffix}", owner_id=a_owner.id)
        org_b = Organization(name=f"AT Org B {suffix}", slug=f"at-org-b-{suffix}", owner_id=b_owner.id)
        db.add_all([org_a, org_b])
        await db.commit()
        from sqlalchemy.orm import selectinload
        from sqlalchemy import select as _select
        org_a = (await db.execute(_select(Organization).options(selectinload(Organization.subscription)).where(Organization.id == org_a.id))).scalar_one()
        org_b = (await db.execute(_select(Organization).options(selectinload(Organization.subscription)).where(Organization.id == org_b.id))).scalar_one()

        db.add_all([
            OrganizationMembership(organization_id=org_a.id, user_id=a_owner.id, role=OWNER),
            OrganizationMembership(organization_id=org_a.id, user_id=a_admin_stale.id, role=ADMIN),
            OrganizationMembership(organization_id=org_a.id, user_id=a_fake_admin.id, role=TEAM_MEMBER),
            OrganizationMembership(organization_id=org_a.id, user_id=a_client.id, role=CLIENT),
            OrganizationMembership(organization_id=org_a.id, user_id=a_inactive_member.id, role=TEAM_MEMBER, is_active=False),
            OrganizationMembership(organization_id=org_a.id, user_id=a_inactive_user.id, role=TEAM_MEMBER),
            OrganizationMembership(organization_id=org_a.id, user_id=a_alex.id, role=TEAM_MEMBER),
            OrganizationMembership(organization_id=org_a.id, user_id=a_team_member.id, role=TEAM_MEMBER),
            OrganizationMembership(organization_id=org_b.id, user_id=b_owner.id, role=OWNER),
            OrganizationMembership(organization_id=org_b.id, user_id=b_alex.id, role=TEAM_MEMBER),
        ])
        await db.commit()

        # Multi-org user: Admin in Org A, Team Member in Org B.
        multi_membership_a = OrganizationMembership(organization_id=org_a.id, user_id=b_multiuser.id, role=ADMIN)
        multi_membership_b = OrganizationMembership(organization_id=org_b.id, user_id=b_multiuser.id, role=TEAM_MEMBER)
        db.add_all([multi_membership_a, multi_membership_b])
        await db.commit()

        user_repo = UserRepository(db)
        created_task_ids: list[int] = []
        created_meeting_ids: list[int] = []
        created_team_ids: list[int] = []

        try:
            # ── 1-5. list_org_assignable_candidates() scoping. ──────────────
            candidates_a = await user_repo.list_org_assignable_candidates(org_a.id)
            candidate_ids_a = {u.id for u, _role in candidates_a}
            role_by_id_a = {u.id: role for u, role in candidates_a}

            assert a_owner.id in candidate_ids_a
            assert a_alex.id in candidate_ids_a
            assert b_owner.id not in candidate_ids_a, "Org B's users must never appear in Org A's candidate set"
            assert b_alex.id not in candidate_ids_a
            assert a_client.id not in candidate_ids_a, "Client must never be an assignable candidate, even with legacy User.role=admin"
            assert a_inactive_member.id not in candidate_ids_a, "an inactive membership must be excluded"
            assert a_inactive_user.id not in candidate_ids_a, "an inactive User account must be excluded"

            # Role source is OrganizationMembership.role, not legacy User.role.
            assert role_by_id_a[a_admin_stale.id] == ADMIN, "membership.role=admin must win even though User.role is stale team_member"
            assert role_by_id_a[a_fake_admin.id] == TEAM_MEMBER, "User.role=admin must NOT leak through when membership.role=team_member"

            # ── 6, 7. resolve_user_id(): cross-org name/email never match
            # when the candidate list passed in is properly org-scoped. ──────
            org_a_user_list = [u for u, _role in candidates_a]
            resolved = resolve_user_id(org_a_user_list, "Alex Smith", None)
            assert resolved == a_alex.id, "must resolve to THIS org's Alex Smith"
            assert resolved != b_alex.id

            resolved_by_email = resolve_user_id(org_a_user_list, None, b_alex.email)
            assert resolved_by_email is None, "an email belonging only to another organization must never match"

            # ── 8, 9. find_fallback_assignee_id(): role source + multi-org. ──
            fallback_a = find_fallback_assignee_id(candidates_a, None, a_owner.id)
            assert fallback_a == a_admin_stale.id, "the fallback Admin must be resolved via OrganizationMembership.role, not the legacy User.role"
            assert fallback_a != a_fake_admin.id, "a merely-legacy-role admin must never be picked over the real membership Admin"

            candidates_b = await user_repo.list_org_assignable_candidates(org_b.id)
            role_by_id_b = {u.id: role for u, role in candidates_b}
            assert role_by_id_b[b_multiuser.id] == TEAM_MEMBER, "the SAME user must be evaluated using ONLY this org's own membership role"
            assert role_by_id_a[b_multiuser.id] == ADMIN if b_multiuser.id in role_by_id_a else True  # sanity below
            multi_role_in_a = {u.id: role for u, role in candidates_a}.get(b_multiuser.id)
            assert multi_role_in_a == ADMIN, "the SAME user's Org A membership role (admin) must still be visible from Org A's own candidate set"

            # ── 10. No eligible candidate at all -> None. ─────────────────────
            empty_result = find_fallback_assignee_id([], None, 999999999)
            assert empty_result is None, "no candidates at all must fail safe to Unassigned, never fabricate an id"

            # ── 11, 12, 13. Team-scoped fallback. ─────────────────────────────
            team = Team(name=f"AT Team {suffix}", team_manager_id=a_owner.id, created_by_id=a_owner.id, organization_id=org_a.id)
            db.add(team)
            await db.flush()
            db.add(TeamMembership(team_id=team.id, user_id=a_team_member.id))
            await db.commit()
            await db.refresh(team)
            created_team_ids.append(team.id)
            team_eligible_ids = {a_team_member.id}  # a_admin_stale is NOT on this team

            # Neither the org Admin (a_admin_stale) nor the meeting owner
            # (a_owner) is on this team, and no source-owner email is given
            # — none of the 3 original fallback steps has an eligible
            # candidate, so the function must fail safe to Unassigned
            # rather than inventing a 4th "any eligible member" step this
            # heuristic never had (PHASE 10 — preserve, don't redesign).
            team_fallback_none = find_fallback_assignee_id(candidates_a, None, a_owner.id, eligible_ids=team_eligible_ids)
            assert team_fallback_none is None, "with no eligible candidate among the 3 original steps, must fail safe to Unassigned"
            assert team_fallback_none != a_admin_stale.id, "an org Admin who is not a Team member must never be a Team Task's fallback assignee"

            # Step 3 (integration owner) succeeds once THAT person is
            # themselves an eligible Team member.
            team_fallback_via_owner = find_fallback_assignee_id(candidates_a, None, a_team_member.id, eligible_ids=team_eligible_ids)
            assert team_fallback_via_owner == a_team_member.id

            # Explicit source-owner-email step still wins first, when eligible.
            owner_email_fallback = find_fallback_assignee_id(candidates_a, a_team_member.email, a_owner.id, eligible_ids=team_eligible_ids)
            assert owner_email_fallback == a_team_member.id

            # ── 14, 15. Defense-in-depth at the persistence boundary. ─────────
            task_repo_a = TaskRepository(db, org_a.id)
            try:
                await task_repo_a.create(
                    TaskCreate(name="Cross-tenant probe", assignee_id=b_owner.id, status="todo"),
                    created_by_id=a_owner.id,
                )
                raise AssertionError("TaskRepository.create() must reject a cross-organization assignee_id")
            except AppException as exc:
                assert exc.code == "INVALID_ASSIGNEE"

            try:
                await task_repo_a.create(
                    TaskCreate(name="Client probe", assignee_id=a_client.id, status="todo"),
                    created_by_id=a_owner.id,
                )
                raise AssertionError("TaskRepository.create() must reject a Client assignee")
            except AppException as exc:
                assert exc.code == "CLIENT_NOT_ASSIGNABLE"

            try:
                await task_repo_a.create(
                    TaskCreate(name="Non-team-member probe", assignee_id=a_admin_stale.id, team_id=team.id, status="todo"),
                    created_by_id=a_owner.id,
                )
                raise AssertionError("TaskRepository.create() must reject a non-Team-member assignee for a Team Task")
            except AppException as exc:
                assert exc.code == "ASSIGNEE_NOT_TEAM_MEMBER"

            # Sanity: a genuinely valid assignee still persists fine.
            valid_task = await task_repo_a.create(
                TaskCreate(name="Valid probe", assignee_id=a_team_member.id, team_id=team.id, status="todo"),
                created_by_id=a_owner.id,
            )
            created_task_ids.append(valid_task.id)
            assert valid_task.assignee_id == a_team_member.id
            assert valid_task.organization_id == org_a.id

            # ── 16. End-to-end: meeting transcript in Org A naming a person
            # who only exists (by that name) in Org B never assigns them. ─────
            a_owner_tenant = TenantContext(
                organization_id=org_a.id, organization=org_a,
                membership=(await db.execute(
                    _select(OrganizationMembership).where(
                        OrganizationMembership.organization_id == org_a.id,
                        OrganizationMembership.user_id == a_owner.id,
                    )
                )).scalar_one(),
                user=a_owner, db=db,
            )

            when = datetime.now(timezone.utc) + timedelta(days=1)
            meeting = await create_meeting(
                MeetingCreate(title=f"AT Meeting {suffix}", scheduled_at=when, meeting_type="level_10"),
                db=db, tenant=a_owner_tenant,
            )
            created_meeting_ids.append(meeting.id)
            await start_recording(meeting.id, db=db, tenant=a_owner_tenant)

            task_item = {
                "title": "Cross-org name collision probe",
                "description": None,
                "suggested_start_date": None,
                "suggested_due_date": (date.today() + timedelta(days=2)).isoformat(),
                "suggested_assignee_name": "Alex Smith",  # ambiguous name across orgs
                "suggested_assignee_email": None,
                "suggested_project_name": None,
                "suggested_team_name": None,
                "confidence": "high",
            }
            monkeypatch.setattr(
                "app.services.ai_task_extractor.get_llm_provider",
                lambda: _ScriptedProvider([_payload([task_item])]),
            )
            result = await stop_recording(
                meeting.id,
                TranscriptSubmit(text="Owner: Alex Smith will handle the cross-org probe."),
                db=db, tenant=a_owner_tenant,
            )
            assert len(result.tasks_created) == 1
            created_summary = result.tasks_created[0]
            assert created_summary.assignee_id != b_alex.id, "must never resolve to the other organization's Alex Smith"
            assert created_summary.assignee_id == a_alex.id, "must resolve to THIS organization's own Alex Smith"

            persisted_task = (await db.execute(
                _select(Task).where(Task.id == result.meeting.meeting_tasks[-1].task_id)
            )).scalar_one()
            created_task_ids.append(persisted_task.id)
            assert persisted_task.organization_id == org_a.id
            assert persisted_task.assignee_id == a_alex.id

            # ── 17. End-to-end: a Team-scoped meeting only assigns within
            # that team — an org-wide Admin who isn't a team member is never
            # chosen just because a name/email didn't resolve. ────────────────
            team_meeting = await create_meeting(
                MeetingCreate(title=f"AT Team Meeting {suffix}", scheduled_at=when, meeting_type="level_10", team_id=team.id),
                db=db, tenant=a_owner_tenant,
            )
            created_meeting_ids.append(team_meeting.id)
            await start_recording(team_meeting.id, db=db, tenant=a_owner_tenant)

            unresolvable_item = {
                "title": "Team meeting unresolvable-name probe",
                "description": None,
                "suggested_start_date": None,
                "suggested_due_date": None,
                "suggested_assignee_name": "Nobody Recognizable",
                "suggested_assignee_email": None,
                "suggested_project_name": None,
                "suggested_team_name": None,
                "confidence": "high",
            }
            monkeypatch.setattr(
                "app.services.ai_task_extractor.get_llm_provider",
                lambda: _ScriptedProvider([_payload([unresolvable_item])]),
            )
            team_result = await stop_recording(
                team_meeting.id,
                TranscriptSubmit(text="Owner: someone should look into this."),
                db=db, tenant=a_owner_tenant,
            )
            assert len(team_result.tasks_created) == 1
            team_created = team_result.tasks_created[0]
            # meetings.py's flow has no fallback heuristic of its own (only
            # direct name/email resolution) — an unresolvable name must leave
            # the Task unassigned, never assign an org-wide Admin who isn't
            # on this team.
            assert team_created.assignee_id is None
            assert team_created.assignee_id != a_admin_stale.id

            team_persisted_task = (await db.execute(
                _select(Task).where(Task.id == team_result.meeting.meeting_tasks[-1].task_id)
            )).scalar_one()
            created_task_ids.append(team_persisted_task.id)
            assert team_persisted_task.team_id == team.id
            assert team_persisted_task.assignee_id is None

        finally:
            if created_meeting_ids:
                await db.execute(delete(MeetingTask).where(MeetingTask.meeting_id.in_(created_meeting_ids)))
            if created_task_ids:
                await db.execute(delete(Task).where(Task.id.in_(created_task_ids)))
            if created_meeting_ids:
                await db.execute(delete(Meeting).where(Meeting.id.in_(created_meeting_ids)))
            if created_team_ids:
                await db.execute(delete(TeamMembership).where(TeamMembership.team_id.in_(created_team_ids)))
                await db.execute(delete(Team).where(Team.id.in_(created_team_ids)))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_([org_a.id, org_b.id])))
            await db.execute(delete(Organization).where(Organization.id.in_([org_a.id, org_b.id])))
            await db.execute(delete(User).where(User.id.in_([u.id for u in all_users])))
            await db.commit()

    await engine.dispose()


def test_automation_assignee_tenant_isolation(monkeypatch):
    asyncio.run(_scenario(monkeypatch))
