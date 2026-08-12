"""Team Manager project-scope access control regression test.

REQUIREMENT (stated directly, superseding the earlier "team-manager
privileges unlock everything" exception documented — and now removed —
from app.core.project_access):
- A Team Manager cannot view or access any project by default.
- If a Team Manager is given access to a specific project by assigning
  them as that project's Project Manager (a normal ProjectMembership row,
  the same mechanism used to assign any Project Manager), they can
  view/access exactly that project — nothing broader.
- A Project Manager can only view/access the project(s) specifically
  assigned to them (unchanged; see test_project_manager_scoped_access.py).

BUG BEING GUARDED AGAINST: a user whose org role was `project_manager` but
who was ALSO granted the `is_team_manager` privilege flag saw ALL projects
in the org (reported live: the sidebar's Projects section listed every
project instead of only the one they were assigned to) — because the old
rule treated any team-manager privilege (base role OR granted flag) as an
automatic broadening to org-wide project visibility. This test proves both
a plain Team Manager (base role) and a Team-Manager-privileged Project
Manager (the exact reported combination) are now scoped correctly, and
that assigning either of them to a specific project via ProjectMembership
grants access to exactly that project.

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
        # The exact reported combination: role=project_manager AND the
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
            # ── Rule 1: neither Team Manager sees ANY project by default. ──
            assert await list_projects(tenant=tm_tenant) == [], "a plain Team Manager must see no projects by default"
            assert await list_projects(tenant=hybrid_tenant) == [], (
                "a Project Manager additionally granted team-manager privileges must NOT get org-wide "
                "project visibility from that flag anymore (this is the exact bug reported live)"
            )

            for tenant in (tm_tenant, hybrid_tenant):
                try:
                    await get_project(project_a.id, tenant=tenant)
                    raise AssertionError("PROJECT_NOT_ASSIGNED must be raised for an unassigned project")
                except AppException as exc:
                    assert exc.code == "PROJECT_NOT_ASSIGNED"

            # ── Rule 2: assigning either of them as Project A's manager
            #    (plain ProjectMembership — no role/flag change) grants
            #    access to exactly that project. ──
            db.add(ProjectMembership(project_id=project_a.id, user_id=plain_tm.id))
            db.add(ProjectMembership(project_id=project_a.id, user_id=hybrid.id))
            await db.commit()

            for tenant in (tm_tenant, hybrid_tenant):
                visible = await list_projects(tenant=tenant)
                assert {p.id for p in visible} == {project_a.id}, (
                    "after being assigned to project A, exactly project A must be visible — never project B, never org-wide"
                )
                fetched = await get_project(project_a.id, tenant=tenant)
                assert fetched.id == project_a.id

                try:
                    await get_project(project_b.id, tenant=tenant)
                    raise AssertionError("project B must still be forbidden — assignment to A must not broaden to org-wide")
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
