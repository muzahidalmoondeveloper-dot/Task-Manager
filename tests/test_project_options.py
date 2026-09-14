"""Regression tests for the canonical org-scoped Project-options endpoint
(GET /projects/options, app.api.routes.projects.list_project_options) —
Team Manager Project-dropdown follow-up.

ROOT CAUSE: a plain Team Manager's Task Create and Rock Create Project
dropdowns were empty. `GET /projects` (list_projects) returns nothing at
all for a plain Team Manager by design (`is_project_management_blocked`
— Project VISIBILITY was conflated with Project MANAGEMENT authority),
and the narrower `GET /projects/for-managed-teams` only returns Projects
already attached to a Team the caller manages via the explicit
Project<->Team association — legitimately empty for a newly-created
managed Team with no Project attached yet.

FIX: `GET /projects/options` — every ACTIVE Project in the current
organization, {id, name} only, available to any non-Client organization
member. Seeing a Project here never implies any Project-management
capability over it (edit/delete/members/Team-attach stay governed by the
existing, separate, unchanged authorization rules).

Covers (spec's "REGRESSION TESTS — PROJECT OPTIONS"):
  21  A pure Team Manager with zero ProjectMembership still sees every
      Project in the organization (Zero Fund / Clarvs / MailHub).
  22  No duplicate options.
  23  Cross-organization Projects are excluded.
  25  Seeing a Project here grants no Project-management/edit permission
      (existing `update_project`/`require_project_management_access`
      rules are untouched and still reject the same TM).
  27  A forged cross-tenant project_id cannot be used to reach/read
      another organization's Project through the normal `GET
      /projects/{id}` route (existing, unchanged 404 behavior — this
      endpoint's broader dropdown visibility does not weaken it).
  Also: Client sees no options at all (no Task/Rock-creation surface
  needs this), and the existing `GET /projects`/`GET
  /projects/for-managed-teams` behavior for a plain Team Manager is
  completely unaffected (still empty/narrow respectively) — this is a
  purely additive third source, never a replacement.

Runs against the real database connection the app uses. Every row this
test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from app.api.routes.projects import get_project, list_project_options, list_projects, list_projects_for_managed_teams, update_project
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import CLIENT, OWNER, TEAM_MANAGER
from app.core.tenant import TenantContext
from app.models.organization import Organization, OrganizationMembership
from app.models.project import Project
from app.models.team import Team
from app.models.user import User
from app.schemas.project import ProjectUpdate


async def _run():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]

        owner = User(full_name="PO Owner", email=f"po.owner.{suffix}@example-corp.com", hashed_password="x", role="owner")
        tm = User(full_name="PO TM", email=f"po.tm.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MANAGER)
        client_user = User(full_name="PO Client", email=f"po.client.{suffix}@example-corp.com", hashed_password="x", role=CLIENT)
        db.add_all([owner, tm, client_user])
        await db.commit()
        for u in (owner, tm, client_user):
            await db.refresh(u)

        org = Organization(name=f"PO Org {suffix}", slug=f"po-org-{suffix}", owner_id=owner.id)
        other_org = Organization(name=f"PO Other Org {suffix}", slug=f"po-other-org-{suffix}", owner_id=owner.id)
        db.add_all([org, other_org])
        await db.commit()
        org = (await db.execute(select(Organization).options(selectinload(Organization.subscription)).where(Organization.id == org.id))).scalar_one()

        memberships = {}
        for u, role in [(owner, OWNER), (tm, TEAM_MANAGER), (client_user, CLIENT)]:
            m = OrganizationMembership(organization_id=org.id, user_id=u.id, role=role)
            db.add(m)
            memberships[u.id] = m
        await db.commit()

        # Pure Team Manager: manages a Team, but has ZERO ProjectMembership
        # anywhere and that Team has no Project attached to it at all.
        team_tech = Team(name=f"PO Tech Team {suffix}", team_manager_id=tm.id, created_by_id=owner.id, organization_id=org.id)
        db.add(team_tech)
        await db.commit()
        await db.refresh(team_tech)

        zero_fund = Project(name=f"Zero Fund {suffix}", created_by_id=owner.id, organization_id=org.id, status="active")
        clarvs = Project(name=f"Clarvs {suffix}", created_by_id=owner.id, organization_id=org.id, status="active")
        mailhub = Project(name=f"MailHub {suffix}", created_by_id=owner.id, organization_id=org.id, status="active")
        cross_tenant_project = Project(name=f"PO CrossTenant {suffix}", created_by_id=owner.id, organization_id=other_org.id, status="active")
        db.add_all([zero_fund, clarvs, mailhub, cross_tenant_project])
        await db.commit()
        for p in (zero_fund, clarvs, mailhub, cross_tenant_project):
            await db.refresh(p)

        owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[owner.id], user=owner, db=db)
        tm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[tm.id], user=tm, db=db)
        client_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[client_user.id], user=client_user, db=db)

        try:
            # ── Sanity: the existing narrower sources remain exactly as
            # they were — empty/narrow for a plain Team Manager — this
            # endpoint is additive, never a replacement. ────────────────
            existing_projects = await list_projects(tenant=tm_tenant)
            assert existing_projects == [], "GET /projects must remain empty for a plain Team Manager (unchanged)"
            managed_team_projects = await list_projects_for_managed_teams(tenant=tm_tenant)
            assert managed_team_projects == [], "GET /projects/for-managed-teams must remain empty (Team not attached to any Project)"

            # ── 21. A pure Team Manager with zero ProjectMembership sees
            # every active Project in the organization. ────────────────
            options = await list_project_options(tenant=tm_tenant)
            names = {o.name for o in options}
            assert names == {zero_fund.name, clarvs.name, mailhub.name}, f"expected exactly the 3 org Projects, got {names}"

            # ── 22. No duplicate options. ────────────────────────────────
            ids = [o.id for o in options]
            assert len(ids) == len(set(ids)), "Project options must be deduplicated"

            # ── 23. Cross-organization Projects are excluded. ───────────
            assert cross_tenant_project.id not in ids
            assert cross_tenant_project.name not in names

            # Owner/Admin sees the same full set too (options is a pure
            # visibility source, not a narrower one for anybody).
            owner_options = await list_project_options(tenant=owner_tenant)
            assert {o.name for o in owner_options} == {zero_fund.name, clarvs.name, mailhub.name}

            # Client gets nothing — no Task/Rock-creation surface needs this.
            client_options = await list_project_options(tenant=client_tenant)
            assert client_options == []

            # ── 25. Seeing a Project in /options grants no Project-
            # management permission — the SAME plain Team Manager is still
            # rejected by the existing, unchanged update_project() rule. ──
            try:
                await update_project(zero_fund.id, ProjectUpdate(name="Hijacked"), tenant=tm_tenant)
                raise AssertionError("a plain Team Manager must still be denied Project-management access")
            except (AppException, HTTPException) as exc:
                assert getattr(exc, "status_code", None) == 403, exc

            # ── 27. A forged cross-tenant project_id is still rejected by
            # the normal single-Project read route — /options's broader
            # dropdown visibility does not weaken this. ──────────────────
            try:
                await get_project(cross_tenant_project.id, tenant=tm_tenant)
                raise AssertionError("a cross-tenant project_id must never be reachable via GET /projects/{id}")
            except AppException as exc:
                assert exc.status_code == 404, exc

        finally:
            await db.execute(delete(Project).where(Project.id.in_([zero_fund.id, clarvs.id, mailhub.id, cross_tenant_project.id])))
            await db.execute(delete(Team).where(Team.id == team_tech.id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id.in_([org.id, other_org.id])))
            await db.execute(delete(User).where(User.id.in_([owner.id, tm.id, client_user.id])))
            await db.commit()

    await engine.dispose()


def test_project_options_org_scoped_for_team_manager():
    asyncio.run(_run())
