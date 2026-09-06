"""Team Manager project-scope access control regression test.

REQUIREMENT (current model — see app.core.project_access's module
docstring for the full rule):
- A plain Team Manager (no explicit Project Manager capability, no Admin)
  cannot view or access ANY project, by default or otherwise — NOT even a
  project they hold a `ProjectMembership` row on. ProjectMembership alone
  is never sufficient authorization for a Team Manager; project access
  must come from explicit Project Manager capability (role or granted
  `is_project_manager` flag) or Admin.
- A user who is BOTH a Project Manager (explicit capability) AND has been
  additionally granted Team Manager privileges keeps normal Project
  Manager behavior for projects: `ProjectMembership` grants them access to
  exactly the project(s) they're assigned to — because they're a genuine
  Project Manager, not because of the Team Manager flag.
- A plain Project Manager (no Team Manager involved) behaves exactly the
  same either way (unchanged; see test_project_manager_scoped_access.py).

BUG HISTORY:
1. Originally, any team-manager privilege (base role OR granted flag) was
   treated as an automatic broadening to org-wide project visibility —
   reported live as a Team-Manager-privileged Project Manager seeing every
   project in the org.
2. That was fixed by making Team Manager "no project access by default,
   unless given a ProjectMembership row" — but that still let a *plain*
   Team Manager (no Project Manager capability at all) gain scoped project
   access merely by holding a ProjectMembership row, which is the bug this
   test now guards against: ProjectMembership must never substitute for
   explicit Project Manager capability. Only a user who is *also* a
   genuine Project Manager gets project access from that membership.

This test proves: a plain Team Manager stays locked out of Projects even
after being given a ProjectMembership row on a project, while a hybrid
Project-Manager-who's-also-a-Team-Manager keeps working exactly like any
other Project Manager once assigned.

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete

from app.api.routes.projects import get_project, list_projects
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import PROJECT_MANAGER, TEAM_MANAGER
from app.core.tenant import TenantContext
from app.models.organization import Organization, OrganizationMembership
from app.models.project import Project, ProjectMembership
from app.models.user import User


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="TMScope Owner", email=f"tmscope.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        plain_tm = User(full_name="TMScope Plain TM", email=f"tmscope.tm.{suffix}@test.invalid", hashed_password="x", role=TEAM_MANAGER)
        hybrid = User(full_name="TMScope Hybrid PM+TM", email=f"tmscope.hybrid.{suffix}@test.invalid", hashed_password="x", role=PROJECT_MANAGER)
        db.add_all([owner, plain_tm, hybrid])
        await db.flush()

        org = Organization(name=f"TMScope Org {suffix}", slug=f"tmscope-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.flush()

        owner_membership = OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner")
        tm_membership = OrganizationMembership(organization_id=org.id, user_id=plain_tm.id, role=TEAM_MANAGER)
        # The exact previously-reported combination: role=project_manager
        # (real, explicit Project Manager capability) AND the
        # is_team_manager privilege flag additionally granted.
        hybrid_membership = OrganizationMembership(organization_id=org.id, user_id=hybrid.id, role=PROJECT_MANAGER, is_team_manager=True)
        db.add_all([owner_membership, tm_membership, hybrid_membership])
        await db.commit()
        for m in (owner_membership, tm_membership, hybrid_membership):
            await db.refresh(m)

        project_a = Project(name=f"Project A {suffix}", created_by_id=owner.id, organization_id=org.id)
        project_b = Project(name=f"Project B {suffix}", created_by_id=owner.id, organization_id=org.id)
        db.add_all([project_a, project_b])
        await db.commit()

        tm_tenant = TenantContext(organization_id=org.id, organization=org, membership=tm_membership, user=plain_tm, db=db)
        hybrid_tenant = TenantContext(organization_id=org.id, organization=org, membership=hybrid_membership, user=hybrid, db=db)

        try:
            # ── Rule 1: neither sees ANY project by default (no
            #    ProjectMembership yet for either). ──
            assert await list_projects(tenant=tm_tenant) == [], "a plain Team Manager must see no projects by default"
            assert await list_projects(tenant=hybrid_tenant) == [], "a Project Manager (even team-manager-privileged) with no ProjectMembership yet sees nothing"

            for tenant in (tm_tenant, hybrid_tenant):
                try:
                    await get_project(project_a.id, tenant=tenant)
                    raise AssertionError("PROJECT_NOT_ASSIGNED must be raised for an unassigned project")
                except AppException as exc:
                    assert exc.code == "PROJECT_NOT_ASSIGNED"

            # ── Rule 2: give BOTH of them a ProjectMembership row on
            #    project A (plain assignment — no role/flag change). ──
            db.add(ProjectMembership(project_id=project_a.id, user_id=plain_tm.id))
            db.add(ProjectMembership(project_id=project_a.id, user_id=hybrid.id))
            await db.commit()

            # The PLAIN Team Manager stays locked out — ProjectMembership
            # alone is never sufficient without explicit Project Manager
            # capability. This is the exact behavior this test now guards.
            assert await list_projects(tenant=tm_tenant) == [], (
                "a plain Team Manager must NOT gain project access merely from holding a ProjectMembership row"
            )
            try:
                await get_project(project_a.id, tenant=tm_tenant)
                raise AssertionError("a plain Team Manager must still be forbidden from a project even with a ProjectMembership row on it")
            except AppException as exc:
                assert exc.code == "PROJECT_NOT_ASSIGNED"

            # The HYBRID user (genuine Project Manager, additionally
            # team-manager-privileged) gets access from their real PM
            # capability — unchanged, exactly like any other PM.
            visible_to_hybrid = await list_projects(tenant=hybrid_tenant)
            assert {p.id for p in visible_to_hybrid} == {project_a.id}, (
                "a genuine Project Manager (even if also team-manager-privileged) must see exactly the project they're assigned to"
            )
            fetched = await get_project(project_a.id, tenant=hybrid_tenant)
            assert fetched.id == project_a.id

            try:
                await get_project(project_b.id, tenant=hybrid_tenant)
                raise AssertionError("project B must still be forbidden — assignment to A must not broaden to org-wide, even for the hybrid user")
            except AppException as exc:
                assert exc.code == "PROJECT_NOT_ASSIGNED"

            # ── Owner is always unrestricted. ──
            owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=owner_membership, user=owner, db=db)
            visible_to_owner = await list_projects(tenant=owner_tenant)
            assert {p.id for p in visible_to_owner} == {project_a.id, project_b.id}

        finally:
            await db.execute(delete(ProjectMembership).where(ProjectMembership.project_id.in_([project_a.id, project_b.id])))
            await db.execute(delete(Project).where(Project.id.in_([project_a.id, project_b.id])))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id == org.id))
            await db.execute(delete(User).where(User.id.in_([owner.id, plain_tm.id, hybrid.id])))
            await db.commit()

    await engine.dispose()


def test_team_manager_and_hybrid_pm_tm_are_scoped_to_assigned_projects_only():
    asyncio.run(_scenario())
