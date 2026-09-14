"""Regression tests for the Team Manager KPI Create Project-dropdown
follow-up.

ROOT CAUSE: KPIsTab.jsx's `load()` called `projectApi.list()` (GET
/projects — Project-management-scoped, empty for a plain Team Manager by
design) instead of the canonical `projectApi.options()` (GET
/projects/options — every active Project in the organization, visibility
only) that Task Create/Rock Create already use. The KPI Create modal's
Project dropdown therefore only ever showed "No project" for a plain Team
Manager with no ProjectMembership.

FIX: swapped the one call site (`projectApi.list()` -> `projectApi.options()`)
in KPIsTab.jsx — no backend change needed. The KPI route's own `project_id`
handling was already correct and is reconfirmed, unchanged, here:
  - `_validate_project()` (app.api.routes.kpi) already validates any
    supplied `project_id` belongs to the caller's own organization —
    ProjectMembership/PM-capability/Project<->Team association were never
    required, matching the product's explicit "Project is just KPI
    context, not KPI-management scope" rule.
  - KPI create/update/delete authorization is unchanged: `require_org_manager`
    (Owner/Admin/Team Manager) + `require_team_access` scoped to the
    KPI's own `team_id` — entirely independent of `project_id`.
  - Seeing a Project here grants no Project-management capability
    (already proven generically in test_project_options.py; reconfirmed
    here against the actual KPI create/update call sites).

Covers spec test items 7, 8, 9, 10, 11, 12 (items 1-6 are the canonical
`GET /projects/options` behavior itself, already covered exhaustively by
test_project_options.py — reused here, not duplicated).

Runs against the real database connection the app uses. Every row this
test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from app.api.routes.kpi import create_kpi, update_kpi
from app.api.routes.projects import list_project_options, update_project
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import OWNER, PROJECT_MANAGER, TEAM_MANAGER, TEAM_MEMBER
from app.core.tenant import TenantContext
from app.models.kpi import KPI
from app.models.organization import Organization, OrganizationMembership
from app.models.project import Project
from app.models.rock import Rock
from app.models.team import Team
from app.models.user import User
from app.schemas.kpi import KPICreate, KPIUpdate
from app.schemas.project import ProjectUpdate


async def _run():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]

        owner = User(full_name="TMKPI Owner", email=f"tmkpi.owner.{suffix}@example-corp.com", hashed_password="x", role="owner")
        tm = User(full_name="TMKPI TM", email=f"tmkpi.tm.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MANAGER)
        member = User(full_name="TMKPI Member", email=f"tmkpi.member.{suffix}@example-corp.com", hashed_password="x", role=TEAM_MEMBER)
        pm = User(full_name="TMKPI PM", email=f"tmkpi.pm.{suffix}@example-corp.com", hashed_password="x", role=PROJECT_MANAGER)
        db.add_all([owner, tm, member, pm])
        await db.commit()
        for u in (owner, tm, member, pm):
            await db.refresh(u)

        org = Organization(name=f"TMKPI Org {suffix}", slug=f"tmkpi-org-{suffix}", owner_id=owner.id)
        other_org = Organization(name=f"TMKPI Other Org {suffix}", slug=f"tmkpi-other-org-{suffix}", owner_id=owner.id)
        db.add_all([org, other_org])
        await db.commit()
        org = (await db.execute(select(Organization).options(selectinload(Organization.subscription)).where(Organization.id == org.id))).scalar_one()

        memberships = {}
        for u, role in [(owner, OWNER), (tm, TEAM_MANAGER), (member, TEAM_MEMBER), (pm, PROJECT_MANAGER)]:
            m = OrganizationMembership(organization_id=org.id, user_id=u.id, role=role)
            db.add(m)
            memberships[u.id] = m
        await db.commit()

        # Pure Team Manager: manages a Team, ZERO ProjectMembership.
        team_tech = Team(name=f"TMKPI Tech {suffix}", team_manager_id=tm.id, created_by_id=owner.id, organization_id=org.id)
        other_team = Team(name=f"TMKPI Other Team {suffix}", team_manager_id=owner.id, created_by_id=owner.id, organization_id=org.id)
        db.add_all([team_tech, other_team])
        await db.commit()
        await db.refresh(team_tech)
        await db.refresh(other_team)

        zero_fund = Project(name=f"Zero Fund {suffix}", created_by_id=owner.id, organization_id=org.id, status="active")
        db.add(zero_fund)
        await db.commit()
        await db.refresh(zero_fund)

        cross_tenant_project = Project(name=f"TMKPI CrossTenant {suffix}", created_by_id=owner.id, organization_id=other_org.id, status="active")
        db.add(cross_tenant_project)
        await db.commit()
        await db.refresh(cross_tenant_project)

        # KPIs require a mandatory Rock link — one Rock per Team involved.
        rock_tech = Rock(title=f"TMKPI Tech Rock {suffix}", team_id=team_tech.id, organization_id=org.id)
        rock_other_team = Rock(title=f"TMKPI Other Team Rock {suffix}", team_id=other_team.id, organization_id=org.id)
        db.add_all([rock_tech, rock_other_team])
        await db.commit()
        await db.refresh(rock_tech)
        await db.refresh(rock_other_team)

        owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[owner.id], user=owner, db=db)
        tm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[tm.id], user=tm, db=db)
        member_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[member.id], user=member, db=db)
        pm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[pm.id], user=pm, db=db)

        created_kpi_ids: list[int] = []

        try:
            # ── 1, 2, 3. The exact dropdown source the KPI modal now uses
            # (reconfirmed against the KPI creation flow itself, not just
            # the endpoint in isolation — see test_project_options.py for
            # the endpoint's own exhaustive coverage). ────────────────────
            options = await list_project_options(tenant=tm_tenant)
            names = {o.name for o in options}
            assert zero_fund.name in names, "a pure Team Manager must see the org's active Projects in the KPI dropdown source"
            assert cross_tenant_project.name not in names

            # ── 7. TM can create a KPI with project_id = NULL. ───────────
            kpi_no_project = await create_kpi(
                team_tech.id,
                KPICreate(title=f"TMKPI No Project {suffix}", owner_id=tm.id, rock_id=rock_tech.id),
                db=db, tenant=tm_tenant,
            )
            created_kpi_ids.append(kpi_no_project.id)
            assert kpi_no_project.project_id is None

            # ── 8. TM can create a KPI linked to a valid same-org Project. ─
            kpi_with_project = await create_kpi(
                team_tech.id,
                KPICreate(title=f"TMKPI With Project {suffix}", owner_id=tm.id, rock_id=rock_tech.id, project_id=zero_fund.id),
                db=db, tenant=tm_tenant,
            )
            created_kpi_ids.append(kpi_with_project.id)
            assert kpi_with_project.project_id == zero_fund.id

            # ── 6. Seeing/using this Project in the KPI dropdown grants no
            # Project-management capability — the SAME TM is still denied
            # by the existing, unchanged update_project() rule. ───────────
            try:
                await update_project(zero_fund.id, ProjectUpdate(name="Hijacked"), tenant=tm_tenant)
                raise AssertionError("linking a KPI to a Project must not grant Project-management access")
            except (AppException, HTTPException) as exc:
                assert getattr(exc, "status_code", None) == 403, exc

            # ── 9. A forged cross-tenant project_id is rejected on KPI
            # create. ────────────────────────────────────────────────────
            try:
                await create_kpi(
                    team_tech.id,
                    KPICreate(title=f"TMKPI Forged {suffix}", owner_id=tm.id, rock_id=rock_tech.id, project_id=cross_tenant_project.id),
                    db=db, tenant=tm_tenant,
                )
                raise AssertionError("a cross-tenant project_id must be rejected on KPI create")
            except HTTPException as exc:
                assert exc.status_code == 422, exc

            # ...and on KPI update.
            try:
                await update_kpi(
                    team_tech.id, kpi_no_project.id,
                    KPIUpdate(project_id=cross_tenant_project.id),
                    db=db, tenant=tm_tenant,
                )
                raise AssertionError("a cross-tenant project_id must be rejected on KPI update")
            except HTTPException as exc:
                assert exc.status_code == 422, exc

            # A nonexistent project_id is rejected too.
            try:
                await create_kpi(
                    team_tech.id,
                    KPICreate(title=f"TMKPI Nonexistent {suffix}", owner_id=tm.id, rock_id=rock_tech.id, project_id=999_999_999),
                    db=db, tenant=tm_tenant,
                )
                raise AssertionError("a nonexistent project_id must be rejected on KPI create")
            except HTTPException as exc:
                assert exc.status_code == 422, exc

            # ── 10. Owner/Admin existing KPI behavior is unchanged — same
            # create/update flow, unrestricted. ─────────────────────────
            owner_kpi = await create_kpi(
                team_tech.id,
                KPICreate(title=f"TMKPI Owner {suffix}", owner_id=owner.id, rock_id=rock_tech.id, project_id=zero_fund.id),
                db=db, tenant=owner_tenant,
            )
            created_kpi_ids.append(owner_kpi.id)
            assert owner_kpi.project_id == zero_fund.id

            # ── 11. Plain roles (Team Member, plain PM) are not
            # accidentally broadened — KPI create still requires
            # require_org_manager (Owner/Admin/Team Manager only). ────────
            try:
                await create_kpi(
                    team_tech.id,
                    KPICreate(title=f"TMKPI Member Attempt {suffix}", owner_id=member.id, rock_id=rock_tech.id),
                    db=db, tenant=member_tenant,
                )
                raise AssertionError("a plain Team Member must still be denied KPI create")
            except (AppException, HTTPException) as exc:
                assert getattr(exc, "status_code", None) == 403, exc
            try:
                await create_kpi(
                    team_tech.id,
                    KPICreate(title=f"TMKPI PM Attempt {suffix}", owner_id=pm.id, rock_id=rock_tech.id),
                    db=db, tenant=pm_tenant,
                )
                raise AssertionError("a plain Project Manager must still be denied KPI create (require_org_manager unchanged)")
            except (AppException, HTTPException) as exc:
                assert getattr(exc, "status_code", None) == 403, exc

            # ── 12. Existing KPI Team-scope authorization is unchanged —
            # the TM still cannot create a KPI under a Team they don't
            # manage, regardless of Project selection. ────────────────────
            try:
                await create_kpi(
                    other_team.id,
                    KPICreate(title=f"TMKPI Wrong Team {suffix}", owner_id=tm.id, rock_id=rock_other_team.id),
                    db=db, tenant=tm_tenant,
                )
                raise AssertionError("a Team Manager must still be denied creating a KPI under a Team they don't manage")
            except (AppException, HTTPException) as exc:
                assert getattr(exc, "status_code", None) == 403, exc

        finally:
            if created_kpi_ids:
                await db.execute(delete(KPI).where(KPI.id.in_(created_kpi_ids)))
            await db.execute(delete(Rock).where(Rock.id.in_([rock_tech.id, rock_other_team.id])))
            await db.execute(delete(Project).where(Project.id.in_([zero_fund.id, cross_tenant_project.id])))
            await db.execute(delete(Team).where(Team.id.in_([team_tech.id, other_team.id])))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id.in_([org.id, other_org.id])))
            await db.execute(delete(User).where(User.id.in_([owner.id, tm.id, member.id, pm.id])))
            await db.commit()

    await engine.dispose()


def test_team_manager_kpi_project_dropdown_and_project_linkage():
    asyncio.run(_run())
