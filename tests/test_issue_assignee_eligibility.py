"""Regression tests for the Global Issues assignee-filtering follow-up.

ROOT CAUSE: `create_issue`/`update_issue` (app.api.routes.issues) never
validated `payload.assignee_id` against the Issue's Team at all — any
active org user id (even one from a different Team, a Client, an inactive
membership, or — since `assignee_id` is a globally-unique user id — a
cross-tenant user) was persisted unchecked. Frontend dropdown filtering
(IssuesPage.jsx) was the ONLY thing narrowing the choice, and even there
it never re-scoped to the selected Team at all — it always showed every
org user regardless of `team_id`.

A second, related bug (Issue-Edit follow-up): `update_issue` applied
payload fields via `payload.model_dump(exclude_none=True, ...)`, which
silently DROPPED any field explicitly sent as `null` — most importantly
`assignee_id: null`, so an Issue could never be changed back to
Unassigned via a PATCH.

FIX:
  - Backend: reuse `app.core.task_assignment.validate_task_assignee` (the
    SAME canonical assignee-eligibility check Tasks/To-Dos already use —
    never a second, Issue-specific reimplementation) in both
    `create_issue` and `update_issue`; switched `update_issue`'s field
    application to `exclude_unset=True` (the same convention
    `update_task` already uses) so an explicit `assignee_id: null` is
    correctly applied instead of silently ignored.
  - Frontend: IssuesPage.jsx's Assignee dropdown now sources from the
    canonical `teamApi.getAssignableUsers(teamId)` (same endpoint
    TeamDetailPage's To-Do modal and TasksPage's Team-scoped Assignee
    dropdown already use) whenever a Team is selected, falling back to
    the existing org-wide eligible-user list only when no Team is
    selected; an Edit Issue action was added to the Global Issues table
    reusing this same `PATCH` endpoint.

This file covers the BACKEND side (the actual enforcement boundary).

Runs against the real database connection the app uses. Every row this
test creates is deleted before it returns.
"""

import asyncio
import uuid

from sqlalchemy import select
from sqlalchemy.orm import selectinload

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete

from app.api.routes.issues import create_issue, update_issue
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import CLIENT, OWNER, TEAM_MANAGER, TEAM_MEMBER
from app.core.tenant import TenantContext
from app.models.issue import Issue
from app.models.organization import Organization, OrganizationMembership
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.schemas.issue import IssueCreate, IssueUpdate


async def _run():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]

        owner = User(full_name="IAE Owner", email=f"iae.owner.{suffix}@example-corp.com", hashed_password="x", role="owner")
        tm = User(full_name="IAE TM", email=f"iae.tm.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MANAGER)
        member_a = User(full_name="IAE Member A", email=f"iae.membera.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)
        member_b = User(full_name="IAE Member B", email=f"iae.memberb.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)
        client_user = User(full_name="IAE Client", email=f"iae.client.{suffix}@example-corp.com", hashed_password="x", role=CLIENT)
        inactive_member = User(full_name="IAE Inactive", email=f"iae.inactive.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)
        db.add_all([owner, tm, member_a, member_b, client_user, inactive_member])
        await db.commit()
        for u in (owner, tm, member_a, member_b, client_user, inactive_member):
            await db.refresh(u)

        org = Organization(name=f"IAE Org {suffix}", slug=f"iae-org-{suffix}", owner_id=owner.id)
        other_org = Organization(name=f"IAE Other Org {suffix}", slug=f"iae-other-org-{suffix}", owner_id=owner.id)
        db.add_all([org, other_org])
        await db.commit()
        org = (await db.execute(select(Organization).options(selectinload(Organization.subscription)).where(Organization.id == org.id))).scalar_one()

        memberships = {}
        for u, role in [(owner, OWNER), (tm, TEAM_MANAGER), (member_a, TEAM_MEMBER), (member_b, TEAM_MEMBER), (client_user, CLIENT), (inactive_member, TEAM_MEMBER)]:
            m = OrganizationMembership(organization_id=org.id, user_id=u.id, role=role)
            db.add(m)
            memberships[u.id] = m
        await db.commit()
        memberships[inactive_member.id].is_active = False
        await db.commit()

        # Cross-tenant user: exists globally, no membership in `org` at all.
        cross_tenant_user = User(full_name="IAE CrossTenant", email=f"iae.crosstenant.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)
        db.add(cross_tenant_user)
        await db.commit()
        await db.refresh(cross_tenant_user)
        db.add(OrganizationMembership(organization_id=other_org.id, user_id=cross_tenant_user.id, role=TEAM_MEMBER))
        await db.commit()

        # Technology Team: tm manages it, member_a belongs to it.
        # Marketing Team: member_b belongs to it instead (member_a does NOT).
        technology = Team(name=f"IAE Technology {suffix}", team_manager_id=tm.id, created_by_id=owner.id, organization_id=org.id)
        marketing = Team(name=f"IAE Marketing {suffix}", team_manager_id=owner.id, created_by_id=owner.id, organization_id=org.id)
        db.add_all([technology, marketing])
        await db.commit()
        for t in (technology, marketing):
            await db.refresh(t)
        db.add_all([
            TeamMembership(team_id=technology.id, user_id=tm.id),
            TeamMembership(team_id=technology.id, user_id=member_a.id),
            TeamMembership(team_id=marketing.id, user_id=member_b.id),
        ])
        await db.commit()

        owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[owner.id], user=owner, db=db)

        created_issue_ids: list[int] = []

        async def _create(team_id, payload):
            issue = await create_issue(team_id, payload, db=db, tenant=owner_tenant)
            created_issue_ids.append(issue.id)
            return issue

        try:
            # ── 1. Eligible Technology Team member -> succeeds. ──────────
            issue1 = await _create(technology.id, IssueCreate(title=f"IAE Issue 1 {suffix}", assignee_id=member_a.id))
            assert issue1.assignee_id == member_a.id

            # ── 2. No assignee (Unassigned) -> succeeds. ─────────────────
            issue2 = await _create(technology.id, IssueCreate(title=f"IAE Issue 2 {suffix}"))
            assert issue2.assignee_id is None

            # ── 3. Wrong-Team member (member_b belongs to Marketing, not
            # Technology) -> rejected on create. ──────────────────────────
            try:
                await _create(technology.id, IssueCreate(title=f"IAE Bad {suffix}", assignee_id=member_b.id))
                raise AssertionError("an assignee from a different Team must be rejected")
            except AppException as exc:
                assert exc.code == "ASSIGNEE_NOT_TEAM_MEMBER", exc

            # ── 4. Client -> rejected. ────────────────────────────────────
            try:
                await _create(technology.id, IssueCreate(title=f"IAE Bad {suffix}", assignee_id=client_user.id))
                raise AssertionError("a Client must never be assignable to an Issue")
            except AppException as exc:
                assert exc.code == "CLIENT_NOT_ASSIGNABLE", exc

            # ── 5. Inactive membership -> rejected. ───────────────────────
            try:
                await _create(technology.id, IssueCreate(title=f"IAE Bad {suffix}", assignee_id=inactive_member.id))
                raise AssertionError("an inactive membership must never be assignable")
            except AppException as exc:
                assert exc.code == "INVALID_ASSIGNEE", exc

            # ── 6. Cross-tenant user -> rejected. ─────────────────────────
            try:
                await _create(technology.id, IssueCreate(title=f"IAE Bad {suffix}", assignee_id=cross_tenant_user.id))
                raise AssertionError("a cross-tenant user id must never be assignable")
            except AppException as exc:
                assert exc.code == "INVALID_ASSIGNEE", exc

            # ── 7. Update: reassigning to a wrong-Team member is rejected. ─
            try:
                await update_issue(technology.id, issue1.id, IssueUpdate(assignee_id=member_b.id), db=db, tenant=owner_tenant)
                raise AssertionError("update_issue must reject a wrong-Team assignee too")
            except AppException as exc:
                assert exc.code == "ASSIGNEE_NOT_TEAM_MEMBER", exc

            # ── 8. Update: reassigning to the SAME Team's other member
            # succeeds, and clearing back to Unassigned succeeds too
            # (Issue-Edit follow-up bug fix: `update_issue` used to apply
            # payload fields via `exclude_none=True`, which silently
            # dropped an explicit `assignee_id: null` — fixed to
            # `exclude_unset=True`, the same convention `update_task`
            # already uses). ─────────────────────────────────────────────
            reassigned = await update_issue(technology.id, issue1.id, IssueUpdate(assignee_id=tm.id), db=db, tenant=owner_tenant)
            assert reassigned.assignee_id == tm.id
            unassigned = await update_issue(technology.id, issue1.id, IssueUpdate(assignee_id=None), db=db, tenant=owner_tenant)
            assert unassigned.assignee_id is None

            # ── 9. Update: moving the Issue to a Team where the CURRENT
            # assignee is no longer eligible is rejected (team_id change +
            # existing assignee mismatch). ─────────────────────────────────
            issue9 = await _create(technology.id, IssueCreate(title=f"IAE Issue 9 {suffix}", assignee_id=member_a.id))
            try:
                await update_issue(technology.id, issue9.id, IssueUpdate(team_id=marketing.id), db=db, tenant=owner_tenant)
                raise AssertionError("moving an Issue to a Team where its current assignee isn't a member must be rejected")
            except AppException as exc:
                assert exc.code == "ASSIGNEE_NOT_TEAM_MEMBER", exc

            # ── 10. Global-Issues Edit follow-up: a full editable-field
            # update (title/description/priority/status) round-trips
            # correctly through the fixed `exclude_unset=True` — every
            # sent field applies, unrelated fields (e.g. the Issue's own
            # assignee, left out of this payload) stay untouched, and the
            # existing `resolved_at` side effect still fires. ─────────────
            issue10 = await _create(technology.id, IssueCreate(title=f"IAE Issue 10 {suffix}", assignee_id=member_a.id))
            edited = await update_issue(
                technology.id, issue10.id,
                IssueUpdate(title="Renamed", description="Updated details", priority=4, status="resolved"),
                db=db, tenant=owner_tenant,
            )
            assert edited.title == "Renamed"
            assert edited.description == "Updated details"
            assert edited.priority == 4
            assert edited.status == "resolved"
            assert edited.resolved_at is not None
            assert edited.assignee_id == member_a.id, "fields omitted from the PATCH must be left untouched"

        finally:
            if created_issue_ids:
                await db.execute(delete(Issue).where(Issue.id.in_(created_issue_ids)))
            await db.execute(delete(TeamMembership).where(TeamMembership.team_id.in_([technology.id, marketing.id])))
            await db.execute(delete(Team).where(Team.id.in_([technology.id, marketing.id])))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_([org.id, other_org.id])))
            await db.execute(delete(Organization).where(Organization.id.in_([org.id, other_org.id])))
            await db.execute(delete(User).where(User.id.in_([owner.id, tm.id, member_a.id, member_b.id, client_user.id, inactive_member.id, cross_tenant_user.id])))
            await db.commit()

    await engine.dispose()


def test_issue_assignee_eligibility():
    asyncio.run(_run())
