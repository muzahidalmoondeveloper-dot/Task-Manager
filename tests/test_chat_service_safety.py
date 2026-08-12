"""Regression tests for two P0 chat_service.py bugs found during the
architecture audit (see MASTER IMPLEMENTATION PROMPT Sections 4.1 / 13):

1. Conversation history was queried `ORDER BY created_at ASC LIMIT N`,
   returning the OLDEST N messages instead of the latest N — any
   conversation longer than the limit was permanently stuck seeing only
   its first N messages, never current context.

2. A missing/malformed/out-of-range intent-classification confidence
   value silently became 1.0 (maximum confidence), which let a
   mutation-risk intent skip the Ambiguity Engine's confirmation gate
   entirely instead of being treated as uncertain.

Runs against the real database connection (same convention as
test_copilot_tenant_isolation.py) since ChatService requires a live DB
session and constructs real repositories in __init__. The LLM provider
itself is swapped for a fake per-scenario to make classification output
deterministic; only chat_service.py's own parsing/validation logic is
under test.

Deliberately ONE `asyncio.run(...)` call for the whole module (not one per
test function) — SQLAlchemy's async engine pools asyncpg connections that
are bound to the event loop that first used them; independent
`asyncio.run()` calls in the same pytest process each start a NEW event
loop, and a pooled connection created under a since-closed loop can get
reused/garbage-collected under a different one, corrupting Windows'
asyncio proactor state ("'NoneType' object has no attribute 'send'"),
which is exactly what happened here before this was consolidated. See
test_copilot_tenant_isolation.py for the same pattern/rationale.
"""

import asyncio

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.

from app.core.database import AsyncSessionLocal, engine
from app.repositories.chat_repository import ChatRepository
from app.services.chat_service import ChatService
from app.services.llm.base import LLMProvider, LLMResponse

ORG_ID = "22e9ed99-5da0-43ad-b217-cce80e0ea343"


class _FakeLLM(LLMProvider):
    # Subclasses the real LLMProvider ABC (rather than being a standalone
    # duck-typed stand-in) so it picks up generate_json()'s base-class
    # default implementation (architecture Section 5/37) for free, matching
    # what every real provider and LLMGateway actually expose — a fake that
    # only implements generate_text() would silently diverge from the real
    # interface the moment a call site (like _detect_intent) starts using
    # generate_json().
    def __init__(self, reply_text: str):
        self._reply_text = reply_text

    async def generate_text(self, **kwargs):
        return LLMResponse(text=self._reply_text)


async def _scenario():
    results = {}

    async with AsyncSessionLocal() as db:
        svc = ChatService(db, ORG_ID)

        svc._llm = _FakeLLM('{"intent": "delete_task"}')  # missing confidence
        _, results["missing"] = await svc._detect_intent("delete everything", "")

        svc._llm = _FakeLLM('{"intent": "delete_task", "confidence": "high"}')  # non-numeric
        _, results["non_numeric"] = await svc._detect_intent("delete everything", "")

        svc._llm = _FakeLLM('{"intent": "delete_task", "confidence": 5.0}')  # out of [0,1]
        _, results["out_of_range"] = await svc._detect_intent("delete everything", "")

        svc._llm = _FakeLLM('{"intent": "delete_task", "confidence": 0.92}')  # valid
        results["valid_intent"], results["valid_confidence"] = await svc._detect_intent("delete everything", "")

    async with AsyncSessionLocal() as db:
        chat_repo = ChatRepository(db, ORG_ID)
        session = await chat_repo.create_session(user_id=2, title="History order regression test")
        try:
            for i in range(1, 26):  # 25 messages; limit below is 20
                await chat_repo.add_message(session.id, "user" if i % 2 else "assistant", f"message #{i}")
            latest_20 = await chat_repo.get_session_messages(session.id, limit=20)
            results["history_contents"] = [m.content for m in latest_20]
        finally:
            await db.delete(session)
            await db.commit()

    # Dispose the pooled connections while still on THIS event loop — the
    # engine is a module-level singleton shared by every test file in the
    # pytest process, and its pooled asyncpg connections are bound to
    # whichever loop first used them. Without this, the next independent
    # `asyncio.run()` (a new loop, e.g. in another test module) can reuse a
    # connection tied to this now-closing loop and corrupt Windows'
    # asyncio proactor state.
    await engine.dispose()
    return results


def test_confidence_and_history_safety():
    results = asyncio.run(_scenario())

    # ── Confidence fail-safe: malformed/missing/out-of-range -> 0.0, never 1.0 ──
    assert results["missing"] == 0.0
    assert results["non_numeric"] == 0.0
    assert results["out_of_range"] == 0.0
    assert results["valid_intent"] == "delete_task"
    assert results["valid_confidence"] == 0.92

    # ── History ordering: latest 20 of 25, chronological, newest last ──
    contents = results["history_contents"]
    assert len(contents) == 20
    assert contents[0] == "message #6"
    assert contents[-1] == "message #25"
    assert contents == sorted(contents, key=lambda c: int(c.split("#")[1]))
