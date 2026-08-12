"""Meeting domain write-tool regression test — Domain Tool Registry level
(schedule_meeting/update_meeting via run_tool()) AND chat-wiring level
(INTENT_MANAGE_MEETING via _route()), matching the two-layer coverage
pattern established for Issues/Rocks/KPI/Client Requests (strict acceptance
audit: "do not count files/classes as implemented unless wired into
chatbot runtime").

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete, select

from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import CLIENT, TEAM_MANAGER
from app.models.meeting import Meeting
from app.models.organization import Organization, OrganizationMembership
from app.models.team import Team
from app.models.user import User
from app.services.chat_service import INTENT_MANAGE_MEETING, ChatService
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

        owner = User(full_name="Meeting Owner", email=f"meeting.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        manager = User(full_name="Meeting Manager", email=f"meeting.mgr.{suffix}@test.invalid", hashed_password="x", role="team_manager")
        db.add_all([owner, manager])
        await db.flush()

        org = Organization(name=f"Meeting Org {suffix}", slug=f"meeting-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.flush()
        db.add_all([
            OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner"),
            OrganizationMembership(organization_id=org.id, user_id=manager.id, role="team_manager"),
        ])
        await db.commit()
        org_id, owner_id, manager_id = org.id, owner.id, manager.id

        team_name = f"Meeting Team {suffix}"
        team = Team(name=team_name, team_manager_id=manager_id, created_by_id=owner_id, organization_id=org_id)
        db.add(team)
        await db.commit()
        team_id = team.id

        created_meeting_ids: list[int] = []

        try:
            # ── Domain Tool Registry level ──
            mgr_ctx = ToolContext(db=db, org_id=org_id, org_role=TEAM_MANAGER, user=manager, user_id=manager_id, session_id=None)
            when = datetime(2026, 3, 15, 15, 0, tzinfo=timezone.utc)
            result = await run_tool(
                "schedule_meeting",
                {"title": f"Registry meeting {suffix}", "scheduled_at": when.isoformat(), "team_id": team_id},
                mgr_ctx,
            )
            assert result.ok, f"schedule_meeting should succeed, got: {result.message}"
            meeting_id = result.data["meeting_id"]
            created_meeting_ids.append(meeting_id)
            row = (await db.execute(select(Meeting).where(Meeting.id == meeting_id))).scalar_one()
            assert row.title == f"Registry meeting {suffix}" and row.team_id == team_id

            new_when = datetime(2026, 3, 16, 10, 0, tzinfo=timezone.utc)
            result = await run_tool("update_meeting", {"meeting_id": meeting_id, "scheduled_at": new_when.isoformat()}, mgr_ctx)
            assert result.ok, f"update_meeting (reschedule) should succeed, got: {result.message}"
            row = (await db.execute(select(Meeting).where(Meeting.id == meeting_id))).scalar_one()
            assert row.scheduled_at == new_when

            result = await run_tool("update_meeting", {"meeting_id": meeting_id, "status": "cancelled"}, mgr_ctx)
            assert result.ok, f"update_meeting (cancel) should succeed, got: {result.message}"
            row = (await db.execute(select(Meeting).where(Meeting.id == meeting_id))).scalar_one()
            assert row.status == "cancelled"

            # ── Chat-wiring level: INTENT_MANAGE_MEETING via _route() ──
            # Done before the soft-failure checks below — those correctly
            # trigger run_tool()'s db.rollback() (the partial-failure fix
            # from an earlier phase), which expires every ORM object still
            # attached to this session, including `manager`; _route() needs
            # the live `manager` object (not just its captured id), so this
            # must run while it's still fresh.
            svc = ChatService(db, org_id)
            svc._llm = _ScriptedExtractionLLM({
                "action": "schedule", "title": f"Wired meeting {suffix}", "scheduled_at": "2026-04-01T09:00:00",
                "duration_minutes": 30, "team_name": team_name, "project_name": None, "location": None,
                "meeting_reference": None, "status": None,
            })
            reply, actions = await svc._route(INTENT_MANAGE_MEETING, manager, "schedule a meeting", "", TEAM_MANAGER, None)
            assert actions, f"manage_meeting schedule must produce an action, got reply: {reply!r}"
            wired_meeting_id = actions[0].payload["meeting_id"]
            created_meeting_ids.append(wired_meeting_id)
            row = (await db.execute(select(Meeting).where(Meeting.id == wired_meeting_id))).scalar_one()
            assert row.title == f"Wired meeting {suffix}" and row.team_id == team_id

            result = await run_tool("update_meeting", {"meeting_id": meeting_id, "status": "not_a_real_status"}, mgr_ctx)
            assert not result.ok, "an invalid meeting status must be rejected"

            # CLIENT is refused for meeting write tools.
            client_owner_ctx = ToolContext(db=db, org_id=org_id, org_role=CLIENT, user=owner, user_id=owner_id, session_id=None)
            result = await run_tool("schedule_meeting", {"title": "Should never exist", "scheduled_at": when.isoformat()}, client_owner_ctx)
            assert not result.ok, "CLIENT must be refused for schedule_meeting"

        finally:
            if created_meeting_ids:
                await db.execute(delete(Meeting).where(Meeting.id.in_(created_meeting_ids)))
            await db.execute(delete(Team).where(Team.id == team_id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org_id))
            await db.execute(delete(Organization).where(Organization.id == org_id))
            await db.execute(delete(User).where(User.id.in_([owner_id, manager_id])))
            await db.commit()

    await engine.dispose()


def test_meeting_write_tools_full_path_and_chat_wiring():
    asyncio.run(_scenario())
