"""Domain buildout chat-wiring regression test — proves the new Issue/Rock/
KPI/Client-Request write tools are actually REACHABLE from natural-language
chat routing (`ChatService._route()`), not just correct in isolation.

This directly closes the strict acceptance audit's own methodology
requirement: "Do not count files/classes as implemented unless they are
actually wired into chatbot runtime." test_domain_buildout_write_tools.py
already proved the tools themselves are correct via run_tool(); this file
proves `_route(INTENT_MANAGE_ISSUE, ...)` etc. — the same entry point
`_detect_and_route_step()` calls after intent classification — reaches
them end to end: LLM extraction → entity resolution (team/project name →
id) → the tool → a verified, persisted database row.

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.
"""

import asyncio
import json
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete, select

from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import CLIENT, TEAM_MANAGER
from app.models.issue import Issue
from app.models.kpi import KPI, KPIEntry
from app.models.organization import Organization, OrganizationMembership
from app.models.project import Project, ProjectMembership
from app.models.rock import Rock
from app.models.task_request import TaskRequest
from app.models.team import Team
from app.models.user import User
from app.services.chat_service import (
    INTENT_MANAGE_ISSUE,
    INTENT_MANAGE_ROCK,
    INTENT_RECORD_KPI,
    INTENT_SUBMIT_CLIENT_REQUEST,
    ChatService,
)
from app.services.llm.base import LLMProvider, LLMResponse


class _ScriptedExtractionLLM(LLMProvider):
    """Returns whatever JSON payload the test wants for the ONE
    generate_structured() call each handler makes."""

    def __init__(self, payload: dict):
        self._payload = json.dumps(payload)

    async def generate_text(self, **kwargs):
        return LLMResponse(text=self._payload)


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="Wiring Owner", email=f"wiring.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        manager = User(full_name="Wiring Manager", email=f"wiring.mgr.{suffix}@test.invalid", hashed_password="x", role="team_manager")
        client = User(full_name="Wiring Client", email=f"wiring.client.{suffix}@test.invalid", hashed_password="x", role="client")
        db.add_all([owner, manager, client])
        await db.flush()

        org = Organization(name=f"Wiring Org {suffix}", slug=f"wiring-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.flush()
        db.add_all([
            OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner"),
            OrganizationMembership(organization_id=org.id, user_id=manager.id, role="team_manager"),
            OrganizationMembership(organization_id=org.id, user_id=client.id, role="client"),
        ])
        await db.commit()
        org_id, owner_id, manager_id, client_id = org.id, owner.id, manager.id, client.id

        team = Team(name=f"Wiring Team {suffix}", team_manager_id=manager_id, created_by_id=owner_id, organization_id=org_id)
        project = Project(name=f"Wiring Project {suffix}", created_by_id=owner_id, organization_id=org_id)
        db.add_all([team, project])
        await db.flush()
        db.add(ProjectMembership(project_id=project.id, user_id=client_id))
        await db.commit()
        team_id, project_id = team.id, project.id

        kpi = KPI(title=f"Wiring KPI {suffix}", team_id=team_id, organization_id=org_id)
        db.add(kpi)
        await db.commit()
        kpi_id = kpi.id

        created_issue_ids: list[int] = []
        created_rock_ids: list[int] = []
        created_kpi_entry_ids: list[int] = []
        created_request_ids: list[int] = []

        try:
            svc = ChatService(db, org_id)

            # ── INTENT_MANAGE_ISSUE via _route(), exactly as _detect_and_route_step() would call it ──
            svc._llm = _ScriptedExtractionLLM({
                "action": "create", "title": f"Wiring issue {suffix}", "team_name": team.name, "project_name": None,
                "issue_reference": None, "status": None, "resolution_plan": None,
            })
            reply, actions = await svc._route(INTENT_MANAGE_ISSUE, manager, "log an issue", "", TEAM_MANAGER, None)
            assert actions, f"manage_issue create must produce an action, got reply: {reply!r}"
            issue_id = actions[0].payload["issue_id"]
            created_issue_ids.append(issue_id)
            row = (await db.execute(select(Issue).where(Issue.id == issue_id))).scalar_one()
            assert row.title == f"Wiring issue {suffix}" and row.team_id == team_id

            # ── INTENT_MANAGE_ROCK via _route() ──
            svc._llm = _ScriptedExtractionLLM({
                "action": "create", "title": f"Wiring rock {suffix}", "team_name": team.name, "project_name": None,
                "rock_reference": None, "status": None, "due_date": None,
            })
            reply, actions = await svc._route(INTENT_MANAGE_ROCK, manager, "create a rock", "", TEAM_MANAGER, None)
            assert actions, f"manage_rock create must produce an action, got reply: {reply!r}"
            rock_id = actions[0].payload["rock_id"]
            created_rock_ids.append(rock_id)
            row = (await db.execute(select(Rock).where(Rock.id == rock_id))).scalar_one()
            assert row.title == f"Wiring rock {suffix}"

            # ── INTENT_RECORD_KPI via _route() — resolves the KPI by name, not just ID ──
            svc._llm = _ScriptedExtractionLLM({
                "kpi_reference": kpi.title, "value": 77.0, "period_type": "monthly", "note": "via chat",
            })
            reply, actions = await svc._route(INTENT_RECORD_KPI, manager, "record 77 for the KPI", "", TEAM_MANAGER, None)
            assert actions, f"record_kpi must produce an action, got reply: {reply!r}"
            entry_id = actions[0].payload["entry_id"]
            created_kpi_entry_ids.append(entry_id)
            row = (await db.execute(select(KPIEntry).where(KPIEntry.id == entry_id))).scalar_one()
            assert row.value == 77.0 and row.period_type == "monthly"

            # ── INTENT_SUBMIT_CLIENT_REQUEST via _route() — CLIENT role, single-project auto-resolution ──
            # A fresh ChatService instance, not the one reused above: _safe_user_id()
            # deliberately caches per-instance (correct in production, where one
            # instance always serves exactly one user's whole turn) — reusing `svc`
            # across a different simulated user here would read back the manager's
            # cached id instead of the client's.
            client_svc = ChatService(db, org_id)
            client_svc._llm = _ScriptedExtractionLLM({
                "title": f"Wiring client request {suffix}", "description": None, "project_name": None,
            })
            reply, actions = await client_svc._route(INTENT_SUBMIT_CLIENT_REQUEST, client, "I'd like to request something", "", CLIENT, None)
            assert actions, f"submit_client_request must produce an action, got reply: {reply!r}"
            request_id = actions[0].payload["request_id"]
            created_request_ids.append(request_id)
            row = (await db.execute(select(TaskRequest).where(TaskRequest.id == request_id))).scalar_one()
            assert row.submitted_by_id == client_id and row.project_id == project_id, (
                "single-accessible-project auto-resolution must pick the client's only project, not leave it unset"
            )

            # ── RBAC still enforced at the _route() gate for the new intents ──
            reply, actions = await svc._route(INTENT_MANAGE_ISSUE, client, "log an issue", "", CLIENT, None)
            assert not actions, "CLIENT must be refused for manage_issue at the _route() coarse gate"

        finally:
            if created_issue_ids:
                await db.execute(delete(Issue).where(Issue.id.in_(created_issue_ids)))
            if created_rock_ids:
                await db.execute(delete(Rock).where(Rock.id.in_(created_rock_ids)))
            if created_kpi_entry_ids:
                await db.execute(delete(KPIEntry).where(KPIEntry.id.in_(created_kpi_entry_ids)))
            if created_request_ids:
                await db.execute(delete(TaskRequest).where(TaskRequest.id.in_(created_request_ids)))
            await db.execute(delete(KPI).where(KPI.id == kpi_id))
            await db.execute(delete(ProjectMembership).where(ProjectMembership.project_id == project_id))
            await db.execute(delete(Project).where(Project.id == project_id))
            await db.execute(delete(Team).where(Team.id == team_id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org_id))
            await db.execute(delete(Organization).where(Organization.id == org_id))
            await db.execute(delete(User).where(User.id.in_([owner_id, manager_id, client_id])))
            await db.commit()

    await engine.dispose()


def test_domain_buildout_intents_reachable_via_route():
    asyncio.run(_scenario())
