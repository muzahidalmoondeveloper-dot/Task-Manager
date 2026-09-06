"""Regression test: KPI and Risk write authorization must be scoped to the
Team Manager's actually-managed team, never any team_id supplied in the URL.

kpi.py audit result: ALREADY CORRECT — every KPI write route (create/
update/delete KPI, create/update/delete KPI group, reorder) calls
app.core.team_access.require_team_access(team_id) via its own local
_require_team() wrapper, layered on top of the coarse require_org_manager
role gate, exactly the "coarse role gate + fine per-resource scope" pattern
used throughout this app. This test proves that existing behavior directly
(a genuine regression check, not a new fix) — a Team Manager can manage
their own team's KPIs and is rejected for another team's.

risks.py audit result: A REAL GAP — create_risk/update_risk/delete_risk
(and list_risks) had NO team-membership check at all, only the coarse
require_org_manager role gate. A Team Manager who legitimately manages
team_a could create/update/delete Risks for ANY team_id, including one
they have no relationship to. Fixed by adding the identical
require_team_access(team_id) check risks.py was missing (see risks.py's
new _require_team() wrapper, mirroring kpi.py's).

Also covers: direct API-manipulation with an arbitrary team_id (not merely
hidden by the frontend) is rejected the same way for both KPI and Risk;
Owner remains unrestricted across both.

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete

from app.api.routes.kpi import create_kpi, update_kpi
from app.api.routes.risks import create_risk, delete_risk, list_risks, update_risk
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import TEAM_MANAGER
from app.core.tenant import TenantContext
from app.models.kpi import KPI
from app.models.organization import Organization, OrganizationMembership
from app.models.risk import Risk
from app.models.rock import Rock
from app.models.team import Team
from app.models.user import User
from app.schemas.kpi import KPICreate, KPIUpdate
from app.schemas.risk import RiskCreate, RiskUpdate


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="KRScope Owner", email=f"krscope.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        team_manager = User(full_name="KRScope TM", email=f"krscope.tm.{suffix}@test.invalid", hashed_password="x", role=TEAM_MANAGER)
        db.add_all([owner, team_manager])
        await db.flush()

        org = Organization(name=f"KRScope Org {suffix}", slug=f"krscope-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.flush()

        owner_membership = OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner")
        tm_membership = OrganizationMembership(organization_id=org.id, user_id=team_manager.id, role=TEAM_MANAGER)
        db.add_all([owner_membership, tm_membership])
        await db.commit()
        for m in (owner_membership, tm_membership):
            await db.refresh(m)

        team_a = Team(name=f"Team A {suffix}", team_manager_id=team_manager.id, created_by_id=owner.id, organization_id=org.id)
        team_b = Team(name=f"Team B {suffix}", team_manager_id=owner.id, created_by_id=owner.id, organization_id=org.id)
        db.add_all([team_a, team_b])
        await db.flush()

        rock_a = Rock(title=f"Rock A {suffix}", team_id=team_a.id, organization_id=org.id)
        rock_b = Rock(title=f"Rock B {suffix}", team_id=team_b.id, organization_id=org.id)
        db.add_all([rock_a, rock_b])
        await db.commit()

        tm_tenant = TenantContext(organization_id=org.id, organization=org, membership=tm_membership, user=team_manager, db=db)
        owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=owner_membership, user=owner, db=db)

        kpi_ids: list[int] = []
        risk_ids: list[int] = []
        try:
            # ── KPI: Team Manager manages team_a's KPIs (already-correct
            #    existing behavior — regression check). ──
            kpi_a = await create_kpi(
                team_a.id, KPICreate(title=f"KPI A {suffix}", rock_id=rock_a.id),
                db=db, tenant=tm_tenant,
            )
            kpi_ids.append(kpi_a.id)
            assert kpi_a.team_id == team_a.id

            updated_a = await update_kpi(
                team_a.id, kpi_a.id, KPIUpdate(title="Renamed KPI A"),
                db=db, tenant=tm_tenant,
            )
            assert updated_a.title == "Renamed KPI A"

            # ── KPI: Team Manager must NOT create/modify team_b's KPIs
            #    (an unmanaged team) — direct API bypass with an arbitrary
            #    team_id, not merely a hidden UI control. ──
            try:
                await create_kpi(team_b.id, KPICreate(title=f"Should fail {suffix}", rock_id=rock_b.id), db=db, tenant=tm_tenant)
                raise AssertionError("a Team Manager must not be able to create a KPI for a team they don't manage")
            except AppException as exc:
                assert exc.code == "TEAM_NOT_ASSIGNED"

            # Owner creates a KPI under team_b so there's a real "another
            # team's KPI" to attempt to modify below.
            kpi_b = await create_kpi(team_b.id, KPICreate(title=f"KPI B {suffix}", rock_id=rock_b.id), db=db, tenant=owner_tenant)
            kpi_ids.append(kpi_b.id)

            try:
                await update_kpi(team_b.id, kpi_b.id, KPIUpdate(title="hacked"), db=db, tenant=tm_tenant)
                raise AssertionError("a Team Manager must not be able to modify another team's KPI")
            except AppException as exc:
                assert exc.code == "TEAM_NOT_ASSIGNED"

            # ── Risk: Team Manager manages team_a's Risks (the fix). ──
            risk_a = await create_risk(team_a.id, RiskCreate(title=f"Risk A {suffix}"), tenant=tm_tenant)
            risk_ids.append(risk_a.id)
            assert risk_a.team_id == team_a.id

            listed_a = await list_risks(team_a.id, status_filter=None, project_id=None, tenant=tm_tenant)
            assert {r.id for r in listed_a} == {risk_a.id}

            updated_risk_a = await update_risk(team_a.id, risk_a.id, RiskUpdate(title="Renamed Risk A"), tenant=tm_tenant)
            assert updated_risk_a.title == "Renamed Risk A"

            # ── Risk: Team Manager must NOT create/list/update/delete
            #    team_b's Risks — this is the exact gap that was fixed. ──
            try:
                await create_risk(team_b.id, RiskCreate(title=f"Should fail {suffix}"), tenant=tm_tenant)
                raise AssertionError("a Team Manager must not be able to create a Risk for a team they don't manage")
            except AppException as exc:
                assert exc.code == "TEAM_NOT_ASSIGNED"

            try:
                await list_risks(team_b.id, status_filter=None, project_id=None, tenant=tm_tenant)
                raise AssertionError("a Team Manager must not be able to list another team's Risks")
            except AppException as exc:
                assert exc.code == "TEAM_NOT_ASSIGNED"

            # Owner creates a Risk under team_b so there's a real "another
            # team's Risk" to attempt to modify/delete below.
            risk_b = await create_risk(team_b.id, RiskCreate(title=f"Risk B {suffix}"), tenant=owner_tenant)
            risk_ids.append(risk_b.id)

            try:
                await update_risk(team_b.id, risk_b.id, RiskUpdate(title="hacked"), tenant=tm_tenant)
                raise AssertionError("a Team Manager must not be able to modify another team's Risk")
            except AppException as exc:
                assert exc.code == "TEAM_NOT_ASSIGNED"

            try:
                await delete_risk(team_b.id, risk_b.id, tenant=tm_tenant)
                raise AssertionError("a Team Manager must not be able to delete another team's Risk")
            except AppException as exc:
                assert exc.code == "TEAM_NOT_ASSIGNED"

            # ── Owner is unrestricted across both teams for both KPI and Risk. ──
            owner_updated_kpi_b = await update_kpi(team_b.id, kpi_b.id, KPIUpdate(title="Owner edited"), db=db, tenant=owner_tenant)
            assert owner_updated_kpi_b.title == "Owner edited"
            owner_updated_risk_b = await update_risk(team_b.id, risk_b.id, RiskUpdate(title="Owner edited"), tenant=owner_tenant)
            assert owner_updated_risk_b.title == "Owner edited"

        finally:
            await db.execute(delete(KPI).where(KPI.id.in_(kpi_ids)))
            await db.execute(delete(Risk).where(Risk.id.in_(risk_ids)))
            await db.execute(delete(Rock).where(Rock.id.in_([rock_a.id, rock_b.id])))
            await db.execute(delete(Team).where(Team.id.in_([team_a.id, team_b.id])))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id == org.id))
            await db.execute(delete(User).where(User.id.in_([owner.id, team_manager.id])))
            await db.commit()

    await engine.dispose()


def test_team_manager_kpi_and_risk_are_scoped_to_their_managed_team():
    asyncio.run(_scenario())
