"""Structured LLM Gateway regression tests (architecture Section 5/37 —
"Structured LLM gateway validation, repair, retry, and fallback for
schema-invalid responses"). Pure in-memory — no database involved, so no
asyncio.run/engine.dispose consolidation concerns apply here (see the other
test files' docstrings for why that matters on Windows for DB-touching
tests)."""

import asyncio

from app.services.llm.base import LLMProvider, LLMResponse
from app.services.llm.gateway import LLMGateway, LLMSchemaError


class _ScriptedProvider(LLMProvider):
    """Returns each of `replies` in order across successive calls; raises
    if called more times than scripted (keeps tests honest about exactly
    how many attempts the gateway actually makes)."""

    def __init__(self, replies: list[str | Exception]):
        self._replies = list(replies)
        self.call_count = 0

    async def generate_text(self, **kwargs):
        if self.call_count >= len(self._replies):
            raise AssertionError(f"_ScriptedProvider called more times than scripted ({self.call_count + 1})")
        reply = self._replies[self.call_count]
        self.call_count += 1
        if isinstance(reply, Exception):
            raise reply
        return LLMResponse(text=reply)


def test_generate_json_succeeds_on_first_valid_response():
    async def scenario():
        primary = _ScriptedProvider(['{"intent": "create_task"}'])
        gateway = LLMGateway(primary)
        data = await gateway.generate_json(user_prompt="hi")
        assert data == {"intent": "create_task"}
        assert primary.call_count == 1

    asyncio.run(scenario())


def test_generate_json_repairs_invalid_json_with_one_retry():
    async def scenario():
        primary = _ScriptedProvider(["not json at all", '{"intent": "delete_task"}'])
        gateway = LLMGateway(primary)
        data = await gateway.generate_json(user_prompt="hi")
        assert data == {"intent": "delete_task"}
        assert primary.call_count == 2, "must have used exactly the one repair retry"

    asyncio.run(scenario())


def test_generate_json_falls_back_to_second_provider_after_repair_exhausted():
    async def scenario():
        primary = _ScriptedProvider(["not json", "still not json"])
        fallback = _ScriptedProvider(['{"intent": "list_tasks"}'])
        gateway = LLMGateway(primary, fallback)
        data = await gateway.generate_json(user_prompt="hi")
        assert data == {"intent": "list_tasks"}
        assert primary.call_count == 2, "primary must exhaust its repair budget before falling back"
        assert fallback.call_count == 1, "fallback must succeed on its first attempt, no repair needed"

    asyncio.run(scenario())


def test_generate_json_raises_schema_error_when_every_provider_exhausted():
    async def scenario():
        primary = _ScriptedProvider(["nope", "still nope"])
        fallback = _ScriptedProvider(["nope too", "nope again"])
        gateway = LLMGateway(primary, fallback)
        raised = False
        try:
            await gateway.generate_json(user_prompt="hi")
        except LLMSchemaError:
            raised = True
        assert raised, "must raise LLMSchemaError, never return an unparsed/guessed result"
        assert primary.call_count == 2
        assert fallback.call_count == 2

    asyncio.run(scenario())


def test_generate_json_moves_to_fallback_immediately_on_provider_exception():
    async def scenario():
        primary = _ScriptedProvider([RuntimeError("provider is down")])
        fallback = _ScriptedProvider(['{"intent": "general"}'])
        gateway = LLMGateway(primary, fallback)
        data = await gateway.generate_json(user_prompt="hi")
        assert data == {"intent": "general"}
        # An exception (not a schema-invalid response) is not repair-retried
        # against the same provider — it moves straight to the next one.
        assert primary.call_count == 1
        assert fallback.call_count == 1

    asyncio.run(scenario())


def test_generate_json_available_on_base_provider_without_a_gateway():
    """A raw LLMProvider (no Gateway wrapping at all) still gets
    generate_json for free via the base-class default — callers must not
    have to know or care whether they're holding a bare provider or a
    Gateway."""
    async def scenario():
        provider = _ScriptedProvider(["bad json", '{"ok": true}'])
        data = await provider.generate_json(user_prompt="hi")
        assert data == {"ok": True}
        assert provider.call_count == 2

    asyncio.run(scenario())
