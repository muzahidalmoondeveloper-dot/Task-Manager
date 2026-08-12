"""Bengali/Banglish contextual reference understanding regression test
(architecture item 7, language-agnostic refactor).

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.

Language-agnostic refactor: chat_service.py used to contain a hardcoded,
hand-maintained Bengali/Banglish word list (Bangla script + several Latin
transliteration spelling variants) that the backend pattern-matched
directly to recognize ordinal/deictic references — a separate, parallel
code path from the English word list next to it, and inherently incomplete
(it could only ever cover the specific words someone thought to add). That
backend-side word-matching has been removed entirely (see
chat_service._resolve_task_reference()'s docstring); the LLM now performs
ALL of the linguistic understanding — for Bengali, Banglish, or any other
language — at extraction time, and reduces it to the same canonical
{"reference_type": ..., "ordinal_position": ...} shape the English path
already produces. There is no Bengali-specific code left in chat_service.py
to test directly.

This test proves that pipeline end-to-end: real Bengali (Bangla script) and
Banglish (Latin-script transliteration) MESSAGES are sent through
ChatService._handle_update_task() exactly as a user would type them, with a
scripted LLM standing in for what a real multilingual-capable LLM would
extract from that text (the same shape used by the golden-set live
evaluation in app/services/copilot/evaluation.py, which exercises the real
configured LLM_PROVIDER against Bengali/Banglish input for intent
classification) — proving the DETERMINISTIC backend resolution is
language-agnostic: identical canonical JSON in, identical resolution
behavior out, regardless of what language produced it.
"""

import asyncio
import json
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete

from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import TEAM_MANAGER
from app.models.chat import ChatSession
from app.models.organization import Organization, OrganizationMembership
from app.models.task import Task
from app.models.user import User
from app.schemas.chat import PageContext
from app.schemas.task import TaskCreate
from app.services.chat_service import ChatService
from app.services.llm.base import LLMProvider, LLMResponse


class _ScriptedCanonicalLLM(LLMProvider):
    """Stands in for a real multilingual LLM's extraction call: given
    whatever Bengali/Banglish message was actually sent to
    _handle_update_task, returns the canonical reference JSON a real LLM
    would have produced after understanding it. chat_service.py never sees
    or inspects the original Bengali/Banglish text for reference
    resolution purposes — only this canonical JSON matters from here on."""
    def __init__(self, canonical: dict):
        self._payload = json.dumps({
            "reference_type": canonical.get("reference_type", "explicit"),
            "task_reference": canonical.get("task_reference"),
            "ordinal_position": canonical.get("ordinal_position"),
            "updates": {"status": canonical.get("status", "done")},
        })

    async def generate_text(self, **kwargs):
        return LLMResponse(text=self._payload)


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="Bengali Test Owner", email=f"bn.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        db.add(owner)
        await db.flush()

        org = Organization(name=f"Bengali Test Org {suffix}", slug=f"bn-test-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.flush()
        db.add(OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner"))
        session = ChatSession(user_id=owner.id, organization_id=org.id)
        db.add(session)
        await db.commit()
        await db.refresh(session)

        svc = ChatService(db, org.id)
        task_1 = await svc._task_repo.create(TaskCreate(name=f"Bengali task alpha {suffix}"), created_by_id=owner.id)
        task_2 = await svc._task_repo.create(TaskCreate(name=f"Bengali task bravo {suffix}"), created_by_id=owner.id)
        task_3 = await svc._task_repo.create(TaskCreate(name=f"Bengali task charlie {suffix}"), created_by_id=owner.id)

        try:
            await svc._store_result_set(session.id, "task", [task_1.id, task_2.id, task_3.id])

            # ── Bangla script ordinal: "দ্বিতীয়টা শেষ করো" ("finish the second one") ──
            svc._llm = _ScriptedCanonicalLLM({"reference_type": "ordinal", "ordinal_position": 2})
            reply, actions = await svc._handle_update_task(
                owner, "দ্বিতীয়টা শেষ করো", session_id=session.id, org_role=TEAM_MANAGER,
            )
            assert actions and actions[0].payload.get("task_id") == task_2.id, (
                f"Bangla-script ordinal ('the second one') must resolve to task_2, got reply: {reply!r}"
            )

            # ── Banglish (Latin-script transliteration) ordinal: "shesh ta done koro" ("mark the last one done") ──
            svc._llm = _ScriptedCanonicalLLM({"reference_type": "ordinal", "ordinal_position": -1})
            reply, actions = await svc._handle_update_task(
                owner, "shesh ta done koro", session_id=session.id, org_role=TEAM_MANAGER,
            )
            assert actions and actions[0].payload.get("task_id") == task_3.id, (
                f"Banglish ordinal ('the last one') must resolve to task_3, got reply: {reply!r}"
            )

            # ── Deictic page-context resolution: Bangla script "এটা শেষ করো" ("finish this one") ──
            svc._page_context = PageContext(page_type="task", entity_id=task_2.id)
            svc._llm = _ScriptedCanonicalLLM({"reference_type": "deictic"})
            reply, actions = await svc._handle_update_task(
                owner, "এটা শেষ করো", org_role=TEAM_MANAGER,
            )
            assert actions and actions[0].payload.get("task_id") == task_2.id, (
                f"Bangla-script deictic ('this one') must resolve via page context to task_2, got reply: {reply!r}"
            )

            # ── Deictic page-context resolution: Banglish "eta complete koro" ──
            svc._llm = _ScriptedCanonicalLLM({"reference_type": "deictic"})
            reply, actions = await svc._handle_update_task(
                owner, "eta complete koro", org_role=TEAM_MANAGER,
            )
            assert actions and actions[0].payload.get("task_id") == task_2.id, (
                f"Banglish deictic ('this one') must resolve via page context to task_2, got reply: {reply!r}"
            )

            # ── Explicit reference in Banglish: naming the task directly still works ──
            svc._page_context = None
            svc._llm = _ScriptedCanonicalLLM({"reference_type": "explicit", "task_reference": task_1.name})
            reply, actions = await svc._handle_update_task(
                owner, f"{task_1.name} ta done koro", session_id=session.id, org_role=TEAM_MANAGER,
            )
            assert actions and actions[0].payload.get("task_id") == task_1.id, (
                f"an explicitly-named task in a Banglish message must still resolve by name, got reply: {reply!r}"
            )

        finally:
            await db.execute(delete(Task).where(Task.id.in_([task_1.id, task_2.id, task_3.id])))
            await db.execute(delete(ChatSession).where(ChatSession.id == session.id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id == org.id))
            await db.execute(delete(User).where(User.id == owner.id))
            await db.commit()

    await engine.dispose()


def test_bengali_and_banglish_references_resolve_via_canonical_llm_extraction():
    asyncio.run(_scenario())
