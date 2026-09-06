"""Regression test: a Team Manager without Admin access must not receive
organization-wide Projects/Users management authority.

ROOT CAUSE: several routes/helpers gated write or list access on
`tenant.is_manager_or_above` alone (true for Owner, Admin, AND Team
Manager) with no further per-project scoping layered on top — unlike the
sibling checks for Project Manager, which always additionally required
`project_repo.is_member(project_id, user_id)`. That let a plain Team
Manager:
  - create brand-new organization-wide projects (POST /projects)
  - view/assign/remove the Project Manager of ANY project in the org
    (GET/POST/DELETE /projects/{id}/members)
  - invite clients to ANY project (app.core.project_permissions
    .require_can_invite_to_project, used by both client_invitations.py and
    project_invitations.py)
  - review/manage client task requests on ANY project
    (app.api.routes.task_requests._require_is_staff)
  - see every client invitation in the org, not just their own projects'
    (GET /client-invitations)

Fix: those write/list paths now require Owner/Admin outright (POST
/projects and the three /projects/{id}/members routes — organization-wide
by nature, not project-scoped in the first place), or additionally require
`project_repo.is_member(...)` for Team Manager exactly like Project
Manager already had to satisfy (the three shared helpers above).

This test exercises the actual dependency/helper functions directly (the
same objects the routes reference via Depends(...) or call inline) against
real DB-loaded TenantContexts — no HTTP client layer needed for a
correctness check like this; Depends() itself is FastAPI-only wiring that
doesn't run when a route function is called directly in a test, so the
*gate function* has to be invoked to actually exercise the check.

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from app.api.routes.client_invitations import list_client_invitations
from app.api.routes.projects import add_project_member, create_project, list_project_members, remove_project_member
from app.api.routes.task_requests import _require_is_staff
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import CLIENT
from app.core.project_permissions import require_can_invite_to_project
from app.core.tenant import TenantContext, require_org_admin, require_org_manager
from app.models.organization import Organization, OrganizationInvitation, OrganizationMembership
from app.models.project import Project, ProjectMembership
from app.models.user import User
from app.repositories.project_repository import ProjectRepository
from app.schemas.project import ProjectCreate, ProjectMemberAssign


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="TMScope Owner", email=f"tmscope.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        team_manager = User(full_name="TMScope TeamManager", email=f"tmscope.tm.{suffix}@test.invalid", hashed_password="x", role="team_manager")
        # role="project_manager" — list_project_members()/add_project_member()
        # only surface Project-Manager-role assignees by design (see that
        # route's own docstring); a team_member added there wouldn't show up
        # in the list even though the membership row itself was created.
        other_user = User(full_name="TMScope Other", email=f"tmscope.other.{suffix}@test.invalid", hashed_password="x", role="project_manager")
        db.add_all([owner, team_manager, other_user])
        await db.flush()

        org = Organization(name=f"TMScope Org {suffix}", slug=f"tmscope-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.flush()
        await db.commit()
        # check_active_billing() (called by create_project) does a plain
        # sync attribute read of tenant.organization.subscription — it must
        # already be loaded, not lazy-loaded from sync code (that raises
        # MissingGreenlet under async SQLAlchemy). get_tenant_context()
        # always eager-loads it the same way in the real app; mirror that
        # here since this test builds TenantContext directly.
        org = (await db.execute(
            select(Organization).where(Organization.id == org.id).options(selectinload(Organization.subscription))
        )).scalar_one()

        owner_membership = OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner")
        tm_membership = OrganizationMembership(organization_id=org.id, user_id=team_manager.id, role="team_manager")
        other_membership = OrganizationMembership(organization_id=org.id, user_id=other_user.id, role="project_manager")
        db.add_all([owner_membership, tm_membership, other_membership])
        await db.commit()
        await db.refresh(owner_membership)
        await db.refresh(tm_membership)

        project_a = Project(name=f"TM Project A {suffix}", created_by_id=owner.id, organization_id=org.id)
        project_b = Project(name=f"TM Project B {suffix}", created_by_id=owner.id, organization_id=org.id)
        db.add_all([project_a, project_b])
        await db.flush()
        # Team Manager is explicitly, legitimately assigned to project_a
        # only (the one scoped grant app.core.project_access allows them).
        db.add(ProjectMembership(project_id=project_a.id, user_id=team_manager.id))
        await db.commit()

        future = datetime.now(timezone.utc) + timedelta(days=7)
        inv_a = OrganizationInvitation(organization_id=org.id, email=f"client.a.{suffix}@test.invalid", role=CLIENT, project_id=project_a.id, invited_by_id=owner.id, token=f"tok-a-{suffix}", expires_at=future)
        inv_b = OrganizationInvitation(organization_id=org.id, email=f"client.b.{suffix}@test.invalid", role=CLIENT, project_id=project_b.id, invited_by_id=owner.id, token=f"tok-b-{suffix}", expires_at=future)
        db.add_all([inv_a, inv_b])
        await db.commit()

        owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=owner_membership, user=owner, db=db)
        tm_tenant = TenantContext(organization_id=org.id, organization=org, membership=tm_membership, user=team_manager, db=db)
        project_repo = ProjectRepository(db, org.id)

        created_project_id = None
        tm_admin_created = None
        try:
            # ── 1. POST /projects (create_project): Team Manager rejected,
            #    Owner unaffected. ──
            try:
                await require_org_admin(tenant=tm_tenant)
                raise AssertionError("a plain Team Manager must not pass require_org_admin")
            except AppException as exc:
                assert exc.code == "ORG_ADMIN_REQUIRED"

            checked = await require_org_admin(tenant=owner_tenant)
            assert checked is owner_tenant

            created = await create_project(ProjectCreate(name=f"TM Created Project {suffix}"), tenant=owner_tenant)
            created_project_id = created.id
            assert created.name == f"TM Created Project {suffix}"

            # ── 2. GET/POST/DELETE /projects/{id}/members: same gate as
            #    #1 (all three routes reference the identical dependency
            #    object) — Owner can still list/add/remove project
            #    members end to end. ──
            members = await list_project_members(project_a.id, tenant=owner_tenant)
            assert members == []

            added = await add_project_member(project_a.id, ProjectMemberAssign(user_id=other_user.id), tenant=owner_tenant)
            assert added.user_id == other_user.id

            members_after = await list_project_members(project_a.id, tenant=owner_tenant)
            assert {m.user_id for m in members_after} == {other_user.id}

            await remove_project_member(project_a.id, other_user.id, tenant=owner_tenant)
            members_final = await list_project_members(project_a.id, tenant=owner_tenant)
            assert members_final == []

            # ── 3. require_can_invite_to_project: Team Manager scoped to
            #    exactly the project they're assigned to, not org-wide. ──
            try:
                await require_can_invite_to_project(tm_tenant, project_repo, project_b.id)
                raise AssertionError("Team Manager must not be able to invite clients to an unrelated project")
            except AppException as exc:
                assert exc.code == "CLIENT_INVITE_FORBIDDEN"

            await require_can_invite_to_project(tm_tenant, project_repo, project_a.id)  # must not raise
            await require_can_invite_to_project(owner_tenant, project_repo, project_b.id)  # Owner unconditional, unaffected

            # ── 4. task_requests._require_is_staff: identical scoping rule. ──
            try:
                await _require_is_staff(tm_tenant, project_repo, project_b.id)
                raise AssertionError("Team Manager must not manage client task requests on an unrelated project")
            except AppException as exc:
                assert exc.code == "TASK_REQUEST_STAFF_ONLY"

            await _require_is_staff(tm_tenant, project_repo, project_a.id)  # must not raise
            await _require_is_staff(owner_tenant, project_repo, project_b.id)  # Owner unconditional, unaffected

            # ── 5. GET /client-invitations: Team Manager sees only their
            #    own project's invitation, Owner sees every invitation. ──
            tm_visible = await list_client_invitations(project_id=None, tenant=tm_tenant)
            assert {i.id for i in tm_visible} == {inv_a.id}, "Team Manager must only see invitations for projects they're assigned to"

            owner_visible = await list_client_invitations(project_id=None, tenant=owner_tenant)
            assert {inv_a.id, inv_b.id} <= {i.id for i in owner_visible}

            # ── 6. Team Manager + Admin: granting is_org_admin restores
            #    full access — Team Manager status itself never blocks it. ──
            tm_membership.is_org_admin = True
            await db.commit()
            await db.refresh(tm_membership)
            tm_admin_tenant = TenantContext(organization_id=org.id, organization=org, membership=tm_membership, user=team_manager, db=db)
            admin_checked = await require_org_admin(tenant=tm_admin_tenant)
            assert admin_checked is tm_admin_tenant
            tm_admin_created = await create_project(ProjectCreate(name=f"TM Admin Created {suffix}"), tenant=tm_admin_tenant)

            await require_can_invite_to_project(tm_admin_tenant, project_repo, project_b.id)  # now unconditional too

            # ── 7. Existing Team Manager team-scoped functionality is
            #    unaffected — require_org_manager (used by team creation
            #    etc.) still passes a *plain* Team Manager (the shared
            #    helper itself was never touched, only which routes
            #    reference it). Re-check on the original, non-admin tenant
            #    object (captured before granting is_org_admin above). ──
            unaffected_tenant = TenantContext(organization_id=org.id, organization=org, membership=OrganizationMembership(organization_id=org.id, user_id=team_manager.id, role="team_manager", is_org_admin=False), user=team_manager, db=db)
            manager_checked = await require_org_manager(tenant=unaffected_tenant)
            assert manager_checked is unaffected_tenant, "require_org_manager itself must still accept a plain Team Manager — only specific Projects/Users routes moved off of it"

        finally:
            await db.execute(delete(OrganizationInvitation).where(OrganizationInvitation.id.in_([inv_a.id, inv_b.id])))
            await db.execute(delete(ProjectMembership).where(ProjectMembership.project_id.in_([project_a.id, project_b.id])))
            project_ids = [project_a.id, project_b.id]
            if created_project_id is not None:
                project_ids.append(created_project_id)
            if tm_admin_created is not None:
                project_ids.append(tm_admin_created.id)
            await db.execute(delete(Project).where(Project.id.in_(project_ids)))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id == org.id))
            await db.execute(delete(User).where(User.id.in_([owner.id, team_manager.id, other_user.id])))
            await db.commit()

    await engine.dispose()


def test_team_manager_without_admin_is_scoped_away_from_org_wide_projects_and_users():
    asyncio.run(_scenario())
