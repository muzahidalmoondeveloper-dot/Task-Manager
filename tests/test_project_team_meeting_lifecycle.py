"""Projects/Teams/Meetings chatbot lifecycle regression test (architecture
item 2 — "complete Projects, Teams, and Meetings chatbot lifecycle
capabilities... with typed tools, RBAC/ABAC, risk/confirmation,
transaction, idempotency/concurrency, verification, audit, and tests").

Covers:
- update_project (AUTO/R2): rename, status change, "archive" -> cancelled,
  RBAC refusal, chat-wiring level.
- update_team (AUTO/R2): rename, RBAC refusal, chat-wiring level.
- reassign_team_manager (CONFIRM/R3): change-set preview -> confirm ->
  Transaction Coordinator apply -> post-write verification -> undo restores
  the previous manager. Chat-wiring level via ChatService._route().
- update_meeting: start (in_progress) and end (completed) lifecycle states.

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.
"""

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete, select

from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import OWNER, TEAM_MEMBER
from app.models.chat import ChatSession
from app.models.meeting import Meeting
from app.models.organization import Organization, OrganizationMembership
from app.models.project import Project
from app.models.team import Team
from app.models.user import User
from app.services.chat_service import (
    INTENT_MANAGE_PROJECT,
    INTENT_MANAGE_TEAM,
    ChatService,
)
from app.services.copilot import change_sets, undo
from app.services.copilot.tools import ToolContext, run_tool
from app.services.copilot.transaction import execute_confirmed_change_set
from app.services.llm.base import LLMProvider, LLMResponse


class _ScriptedExtractionLLM(LLMProvider):
    def __init__(self, payload: dict):
        self._payload = json.dumps(payload)

    async def generate_text(self, **kwargs):
        return LLMResponse(text=self._payload)


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="Lifecycle Owner", email=f"lifecycle.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        member = User(full_name="Lifecycle Member", email=f"lifecycle.member.{suffix}@test.invalid", hashed_password="x", role="team_member")
        new_manager = User(full_name="Lifecycle NewManager", email=f"lifecycle.newmgr.{suffix}@test.invalid", hashed_password="x", role="team_manager")
        db.add_all([owner, member, new_manager])
        await db.flush()

        org = Organization(name=f"Lifecycle Org {suffix}", slug=f"lifecycle-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.flush()
        db.add_all([
            OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner"),
            OrganizationMembership(organization_id=org.id, user_id=member.id, role="team_member"),
            OrganizationMembership(organization_id=org.id, user_id=new_manager.id, role="team_manager"),
        ])
        session = ChatSession(user_id=owner.id, organization_id=org.id)
        db.add(session)
        await db.commit()
        await db.refresh(session)
        org_id, owner_id, member_id, new_manager_id, session_id = (
            org.id, owner.id, member.id, new_manager.id, session.id
        )

        project = Project(name=f"Lifecycle Project {suffix}", created_by_id=owner_id, organization_id=org_id)
        team = Team(name=f"Lifecycle Team {suffix}", team_manager_id=owner_id, created_by_id=owner_id, organization_id=org_id)
        meeting = Meeting(
            title=f"Lifecycle Meeting {suffix}", scheduled_at=datetime.now(timezone.utc) + timedelta(hours=1),
            organizer_id=owner_id, organization_id=org_id,
        )
        db.add_all([project, team, meeting])
        await db.commit()
        project_id, team_id, meeting_id = project.id, team.id, meeting.id

        try:
            owner_ctx = ToolContext(db=db, org_id=org_id, org_role=OWNER, user=owner, user_id=owner_id, session_id=session_id)

            # ── update_project: rename + status change (AUTO/R2) ──
            result = await run_tool(
                "update_project", {"project_id": project_id, "name": f"Renamed Project {suffix}", "description": None, "status": "paused"}, owner_ctx,
            )
            assert result.ok, f"update_project should succeed, got: {result.message}"
            row = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one()
            assert row.name == f"Renamed Project {suffix}" and row.status == "paused"

            # ── "archive" -> status=cancelled ──
            result = await run_tool("update_project", {"project_id": project_id, "status": "cancelled"}, owner_ctx)
            assert result.ok
            row = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one()
            assert row.status == "cancelled", "archiving a project must set status=cancelled"

            # ── RBAC: TEAM_MEMBER refused for update_project ──
            member_ctx = ToolContext(db=db, org_id=org_id, org_role=TEAM_MEMBER, user=member, user_id=member_id, session_id=session_id)
            refused = await run_tool("update_project", {"project_id": project_id, "name": "Should never apply"}, member_ctx)
            assert not refused.ok, "TEAM_MEMBER must be refused for update_project"

            # ── update_team: rename (AUTO/R2) ──
            result = await run_tool("update_team", {"team_id": team_id, "name": f"Renamed Team {suffix}", "description": "new description"}, owner_ctx)
            assert result.ok, f"update_team should succeed, got: {result.message}"
            row = (await db.execute(select(Team).where(Team.id == team_id))).scalar_one()
            assert row.name == f"Renamed Team {suffix}" and row.description == "new description"

            refused = await run_tool("update_team", {"team_id": team_id, "name": "Should never apply"}, member_ctx)
            assert not refused.ok, "TEAM_MEMBER must be refused for update_team"

            # ── reassign_team_manager: CONFIRM-tier change-set + transaction ──
            team_row = (await db.execute(select(Team).where(Team.id == team_id))).scalar_one()
            change_set = await change_sets.build_change_set_for_entities(
                db, org_id=org_id, session_id=session_id, user_id=owner_id,
                tool_name="reassign_team_manager",
                params={"team_id": team_id, "new_manager_id": new_manager_id},
                affected=[("team", team_id, team_row.updated_at)],
                affected_summary=f'"{team_row.name}": new manager',
            )
            await db.commit()

            fetched = await change_sets.get_change_set_for_update(db, org_id, change_set.id)
            tx_result = await execute_confirmed_change_set(db, org_id=org_id, org_role=OWNER, change_set=fetched)
            assert tx_result.success, f"reassign_team_manager change-set execution should succeed, got: {tx_result.message}"
            assert tx_result.operation_id is not None, "a reversible action must register an undo operation"

            row = (await db.execute(select(Team).where(Team.id == team_id))).scalar_one()
            assert row.team_manager_id == new_manager_id, "team_manager_id must actually be updated (post-write verification passed)"

            # ── Undo restores the previous manager ──
            op = await undo.get_operation(db, org_id, tx_result.operation_id)
            undo_message = await undo.execute_undo(db, org_id, op)
            assert "previous manager" in undo_message.lower()
            row = (await db.execute(select(Team).where(Team.id == team_id))).scalar_one()
            assert row.team_manager_id == owner_id, "undo must restore the team's previous manager"

            # ── Chat-wiring level: manage_project (AUTO) ──
            proj_svc = ChatService(db, org_id)
            proj_svc._llm = _ScriptedExtractionLLM({
                "project_reference": str(project_id), "name": None, "description": None, "status": "active",
            })
            reply, actions = await proj_svc._route(INTENT_MANAGE_PROJECT, owner, "reactivate the project", "", OWNER, session_id)
            assert actions, f"manage_project intent must produce an action, got reply: {reply!r}"
            row = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one()
            assert row.status == "active"

            # ── Chat-wiring level: manage_team update (AUTO) ──
            team_svc = ChatService(db, org_id)
            team_svc._llm = _ScriptedExtractionLLM({
                "action": "update", "team_reference": str(team_id), "name": f"Chat-Renamed Team {suffix}", "description": None, "new_manager_name": None,
            })
            reply, actions = await team_svc._route(INTENT_MANAGE_TEAM, owner, "rename the team", "", OWNER, session_id)
            assert actions, f"manage_team update must produce an action, got reply: {reply!r}"
            row = (await db.execute(select(Team).where(Team.id == team_id))).scalar_one()
            assert row.name == f"Chat-Renamed Team {suffix}"

            # ── Chat-wiring level: manage_team reassign_manager (CONFIRM preview) ──
            reassign_svc = ChatService(db, org_id)
            reassign_svc._llm = _ScriptedExtractionLLM({
                "action": "reassign_manager", "team_reference": str(team_id), "name": None, "description": None,
                "new_manager_name": new_manager.full_name,
            })
            reply, actions = await reassign_svc._route(INTENT_MANAGE_TEAM, owner, "make the new manager lead this team", "", OWNER, session_id)
            assert actions and actions[0].type == "change_set_preview", (
                f"manage_team reassign_manager must return a change_set_preview action, got: {actions!r} / reply={reply!r}"
            )
            # Manager must NOT have changed yet — only a preview was created.
            row = (await db.execute(select(Team).where(Team.id == team_id))).scalar_one()
            assert row.team_manager_id == owner_id, "reassign_manager must not apply until the change set is confirmed"
            preview_change_set_id = actions[0].payload["change_set_id"]

            # ── Meeting lifecycle: start (in_progress) then end (completed) ──
            result = await run_tool("update_meeting", {"meeting_id": meeting_id, "status": "in_progress"}, owner_ctx)
            assert result.ok, f"starting a meeting should succeed, got: {result.message}"
            row = (await db.execute(select(Meeting).where(Meeting.id == meeting_id))).scalar_one()
            assert row.status == "in_progress"

            result = await run_tool("update_meeting", {"meeting_id": meeting_id, "status": "completed"}, owner_ctx)
            assert result.ok, f"ending a meeting should succeed, got: {result.message}"
            row = (await db.execute(select(Meeting).where(Meeting.id == meeting_id))).scalar_one()
            assert row.status == "completed"

            # An invalid status is rejected, not silently accepted.
            result = await run_tool("update_meeting", {"meeting_id": meeting_id, "status": "bogus_status"}, owner_ctx)
            assert not result.ok, "an invalid meeting status must be rejected"

        finally:
            # Cancel the still-pending preview change set so nothing lingers.
            try:
                pending = await change_sets.get_change_set(db, org_id, preview_change_set_id)
                if pending is not None and pending.status == "pending":
                    await change_sets.cancel_change_set(db, pending)
            except Exception:
                pass
            await db.execute(delete(Meeting).where(Meeting.id == meeting_id))
            await db.execute(delete(Project).where(Project.id == project_id))
            await db.execute(delete(Team).where(Team.id == team_id))
            await db.execute(delete(ChatSession).where(ChatSession.id == session_id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org_id))
            await db.execute(delete(Organization).where(Organization.id == org_id))
            await db.execute(delete(User).where(User.id.in_([owner_id, member_id, new_manager_id])))
            await db.commit()

    await engine.dispose()


def test_project_team_meeting_lifecycle():
    asyncio.run(_scenario())
