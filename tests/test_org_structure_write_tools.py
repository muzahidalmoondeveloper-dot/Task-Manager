"""Project/Team creation regression test — Domain Tool Registry level
(create_project/create_team via run_tool()) AND chat-wiring level
(INTENT_CREATE_PROJECT/INTENT_CREATE_TEAM via _route()), closing the strict
acceptance audit's "Projects/Teams reads bypass the Tool Registry" +
"no write capability" findings.

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.
"""

import asyncio
import json
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete, select

from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import OWNER, TEAM_MEMBER
from app.models.organization import Organization, OrganizationMembership
from app.models.project import Project
from app.models.team import Team
from app.models.user import User
from app.services.chat_service import INTENT_CREATE_PROJECT, INTENT_CREATE_TEAM, ChatService
from app.services.copilot.tools import ToolContext, run_tool
from app.services.llm.base import LLMProvider, LLMResponse


class _ScriptedExtractionLLM(LLMProvider):
    def __init__(self, payload: dict):
        self._payload = json.dumps(payload)

    async def generate_text(self, **kwargs):
        return LLMResponse(text=self._payload)


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="OrgStruct Owner", email=f"orgstruct.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        member = User(full_name="OrgStruct Member", email=f"orgstruct.member.{suffix}@test.invalid", hashed_password="x", role="team_member")
        db.add_all([owner, member])
        await db.flush()

        org = Organization(name=f"OrgStruct Org {suffix}", slug=f"orgstruct-{suffix}", owner_id=owner.id, plan="starter")
        db.add(org)
        await db.flush()
        db.add_all([
            OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner"),
            OrganizationMembership(organization_id=org.id, user_id=member.id, role="team_member"),
        ])
        await db.commit()
        org_id, owner_id, member_id = org.id, owner.id, member.id

        created_project_ids: list[int] = []
        created_team_ids: list[int] = []

        try:
            # ── Domain Tool Registry level ──
            owner_ctx = ToolContext(db=db, org_id=org_id, org_role=OWNER, user=owner, user_id=owner_id, session_id=None)
            result = await run_tool("create_project", {"name": f"Registry Project {suffix}"}, owner_ctx)
            assert result.ok, f"create_project should succeed for owner, got: {result.message}"
            project_id = result.data["project_id"]
            created_project_ids.append(project_id)
            row = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one()
            assert row.name == f"Registry Project {suffix}" and row.created_by_id == owner_id

            result = await run_tool(
                "create_team", {"name": f"Registry Team {suffix}", "team_manager_id": member_id}, owner_ctx,
            )
            assert result.ok, f"create_team should succeed for owner, got: {result.message}"
            team_id = result.data["team_id"]
            created_team_ids.append(team_id)
            row = (await db.execute(select(Team).where(Team.id == team_id))).scalar_one()
            assert row.name == f"Registry Team {suffix}" and row.team_manager_id == member_id

            # ── Chat-wiring level (do this before the RBAC refusal below, which
            #    triggers a rollback that would expire `owner`/`member`) ──
            svc = ChatService(db, org_id)
            svc._llm = _ScriptedExtractionLLM({"name": f"Wired Project {suffix}", "description": None})
            reply, actions = await svc._route(INTENT_CREATE_PROJECT, owner, "create a project", "", OWNER, None)
            assert actions, f"create_project intent must produce an action, got reply: {reply!r}"
            wired_project_id = actions[0].payload["project_id"]
            created_project_ids.append(wired_project_id)

            team_svc = ChatService(db, org_id)
            team_svc._llm = _ScriptedExtractionLLM({
                "name": f"Wired Team {suffix}", "description": None, "team_manager_name": member.full_name,
            })
            reply, actions = await team_svc._route(INTENT_CREATE_TEAM, owner, "create a team", "", OWNER, None)
            assert actions, f"create_team intent must produce an action, got reply: {reply!r}"
            wired_team_id = actions[0].payload["team_id"]
            created_team_ids.append(wired_team_id)
            row = (await db.execute(select(Team).where(Team.id == wired_team_id))).scalar_one()
            assert row.team_manager_id == member_id, "team_manager_name must resolve to the real user by name, not require an ID"

            # ── RBAC: team_member is refused for org-structure creation ──
            member_ctx = ToolContext(db=db, org_id=org_id, org_role=TEAM_MEMBER, user=member, user_id=member_id, session_id=None)
            result = await run_tool("create_project", {"name": "Should never exist"}, member_ctx)
            assert not result.ok, "team_member must be refused for create_project"
            result = await run_tool("create_team", {"name": "Should never exist", "team_manager_id": member_id}, member_ctx)
            assert not result.ok, "team_member must be refused for create_team"

        finally:
            if created_project_ids:
                await db.execute(delete(Project).where(Project.id.in_(created_project_ids)))
            if created_team_ids:
                await db.execute(delete(Team).where(Team.id.in_(created_team_ids)))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org_id))
            await db.execute(delete(Organization).where(Organization.id == org_id))
            await db.execute(delete(User).where(User.id.in_([owner_id, member_id])))
            await db.commit()

    await engine.dispose()


def test_org_structure_write_tools_full_path_and_chat_wiring():
    asyncio.run(_scenario())
