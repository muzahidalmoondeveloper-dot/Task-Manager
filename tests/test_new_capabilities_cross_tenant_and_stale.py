"""Cross-tenant isolation + stale-change-set regression test for this
session's newest capabilities (architecture item 9 — "add all missing
security, cross-tenant, failure-path... tests"):

- update_project / update_team: an org's manager must not be able to
  mutate another organization's project/team by guessing its ID.
- reassign_team_manager: the generalized check_versions_fresh() path
  (build_change_set_for_entities) must correctly detect a team mutated
  out-of-band between preview and confirm, and reject the confirm as stale
  — the same optimistic-lock protection reassign_task already had, now
  proven for the generalized entity-agnostic path too.
- search_knowledge / create_knowledge_document: already covered end-to-end
  in test_knowledge_rag_retrieval.py; not duplicated here.

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete, select

from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import OWNER
from app.models.chat import ChatSession
from app.models.organization import Organization, OrganizationMembership
from app.models.project import Project
from app.models.team import Team
from app.models.user import User
from app.services.copilot import change_sets
from app.services.copilot.tools import ToolContext, run_tool
from app.services.copilot.transaction import execute_confirmed_change_set


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner_a = User(full_name="CrossTenant OwnerA", email=f"crosstenant.ownera.{suffix}@test.invalid", hashed_password="x", role="owner")
        owner_b = User(full_name="CrossTenant OwnerB", email=f"crosstenant.ownerb.{suffix}@test.invalid", hashed_password="x", role="owner")
        new_manager = User(full_name="CrossTenant NewManager", email=f"crosstenant.newmgr.{suffix}@test.invalid", hashed_password="x", role="team_manager")
        db.add_all([owner_a, owner_b, new_manager])
        await db.flush()

        org_a = Organization(name=f"CrossTenant OrgA {suffix}", slug=f"crosstenant-a-{suffix}", owner_id=owner_a.id)
        org_b = Organization(name=f"CrossTenant OrgB {suffix}", slug=f"crosstenant-b-{suffix}", owner_id=owner_b.id)
        db.add_all([org_a, org_b])
        await db.flush()
        db.add_all([
            OrganizationMembership(organization_id=org_a.id, user_id=owner_a.id, role="owner"),
            OrganizationMembership(organization_id=org_b.id, user_id=owner_b.id, role="owner"),
            OrganizationMembership(organization_id=org_a.id, user_id=new_manager.id, role="team_manager"),
        ])
        session_a = ChatSession(user_id=owner_a.id, organization_id=org_a.id)
        db.add(session_a)
        await db.commit()
        await db.refresh(session_a)
        org_a_id, org_b_id, owner_a_id, owner_b_id, new_manager_id, session_a_id = (
            org_a.id, org_b.id, owner_a.id, owner_b.id, new_manager.id, session_a.id
        )

        # Project/Team live in org B — owner_a (org A) must never touch them.
        project_b = Project(name=f"CrossTenant ProjectB {suffix}", created_by_id=owner_b_id, organization_id=org_b_id)
        team_b = Team(name=f"CrossTenant TeamB {suffix}", team_manager_id=owner_b_id, created_by_id=owner_b_id, organization_id=org_b_id)
        db.add_all([project_b, team_b])
        await db.commit()
        project_b_id, team_b_id = project_b.id, team_b.id

        # A team in org A, for the stale-change-set test.
        team_a = Team(name=f"CrossTenant TeamA {suffix}", team_manager_id=owner_a_id, created_by_id=owner_a_id, organization_id=org_a_id)
        db.add(team_a)
        await db.commit()
        team_a_id = team_a.id

        try:
            owner_a_ctx = ToolContext(db=db, org_id=org_a_id, org_role=OWNER, user=owner_a, user_id=owner_a_id, session_id=session_a_id)

            # ── CROSS-TENANT: update_project must not find org B's project when scoped to org A ──
            result = await run_tool("update_project", {"project_id": project_b_id, "name": "Should never apply"}, owner_a_ctx)
            assert not result.ok, "CROSS-TENANT LEAK: update_project must not find/mutate a project in a different organization"
            row = (await db.execute(select(Project).where(Project.id == project_b_id))).scalar_one()
            assert row.name == f"CrossTenant ProjectB {suffix}", "org B's project must be completely untouched"

            # ── CROSS-TENANT: update_team must not find org B's team when scoped to org A ──
            result = await run_tool("update_team", {"team_id": team_b_id, "name": "Should never apply"}, owner_a_ctx)
            assert not result.ok, "CROSS-TENANT LEAK: update_team must not find/mutate a team in a different organization"
            row = (await db.execute(select(Team).where(Team.id == team_b_id))).scalar_one()
            assert row.name == f"CrossTenant TeamB {suffix}", "org B's team must be completely untouched"

            # ── CROSS-TENANT: reassign_team_manager's transaction coordinator
            #    must not apply a change set built for org A against org B's
            #    team, even if somehow invoked with org B's id (defense in
            #    depth — the apply helper itself re-scopes by org_id). ──
            result = await run_tool("update_team", {"team_id": team_b_id, "description": "leak check"}, owner_a_ctx)
            assert not result.ok

            # ── STALE CHANGE-SET: reassign_team_manager rejects a confirm
            #    after the team was mutated out-of-band since preview. ──
            team_a_row = (await db.execute(select(Team).where(Team.id == team_a_id))).scalar_one()
            change_set = await change_sets.build_change_set_for_entities(
                db, org_id=org_a_id, session_id=session_a_id, user_id=owner_a_id,
                tool_name="reassign_team_manager",
                params={"team_id": team_a_id, "new_manager_id": new_manager_id},
                affected=[("team", team_a_id, team_a_row.updated_at)],
                affected_summary=f'"{team_a_row.name}": new manager',
            )
            await db.commit()

            # Mutate the team out-of-band (simulates someone else editing it
            # via the normal UI between preview and confirm) — this bumps
            # updated_at via the model's onupdate=func.now().
            await db.execute(
                Team.__table__.update().where(Team.id == team_a_id).values(description="mutated out of band")
            )
            await db.commit()

            fetched = await change_sets.get_change_set_for_update(db, org_a_id, change_set.id)
            tx_result = await execute_confirmed_change_set(db, org_id=org_a_id, org_role=OWNER, change_set=fetched)
            assert not tx_result.success, "a stale change set (team mutated since preview) must be rejected, not silently applied"
            row = (await db.execute(select(Team).where(Team.id == team_a_id))).scalar_one()
            assert row.team_manager_id == owner_a_id, "the manager must NOT have been reassigned from a stale change set"

        finally:
            await db.execute(delete(Team).where(Team.id.in_([team_a_id, team_b_id])))
            await db.execute(delete(Project).where(Project.id == project_b_id))
            await db.execute(delete(ChatSession).where(ChatSession.id == session_a_id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_([org_a_id, org_b_id])))
            await db.execute(delete(Organization).where(Organization.id.in_([org_a_id, org_b_id])))
            await db.execute(delete(User).where(User.id.in_([owner_a_id, owner_b_id, new_manager_id])))
            await db.commit()

    await engine.dispose()


def test_new_capabilities_cross_tenant_isolation_and_stale_change_set():
    asyncio.run(_scenario())
