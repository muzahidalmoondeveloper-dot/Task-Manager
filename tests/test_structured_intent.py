"""Structured semantic understanding regression tests (architecture item 4
— "Structured semantic understanding replacing the fragile overlapping
intent pipeline"; item 5's generate_structured()).

Pure in-memory — no database involved (same rationale as test_llm_gateway.py).

Covers: generate_structured() repairs a schema-invalid response (valid JSON,
but a value outside the closed intent/sub_intent set, or a validation
failure like out-of-range confidence) via the same repair-retry machinery as
malformed JSON; a genuinely invented/typo'd intent value is never silently
returned — it either gets corrected on retry or raises LLMSchemaError, never
passed through as-is the way a bare dict + `data.get("intent")` used to.
"""

import asyncio

from app.services.copilot.intent_schemas import DbQueryExtraction, IntentDetectionResult
from app.services.llm.base import LLMProvider, LLMResponse
from app.services.llm.gateway import LLMGateway, LLMSchemaError


class _ScriptedProvider(LLMProvider):
    def __init__(self, replies: list[str]):
        self._replies = list(replies)
        self.call_count = 0

    async def generate_text(self, **kwargs):
        if self.call_count >= len(self._replies):
            raise AssertionError(f"_ScriptedProvider called more times than scripted ({self.call_count + 1})")
        reply = self._replies[self.call_count]
        self.call_count += 1
        return LLMResponse(text=reply)


def test_generate_structured_accepts_a_valid_intent_on_first_try():
    async def scenario():
        provider = _ScriptedProvider(['{"intent": "create_task", "confidence": 0.9}'])
        gateway = LLMGateway(provider)
        result = await gateway.generate_structured(user_prompt="hi", schema=IntentDetectionResult)
        assert result.intent == "create_task"
        assert result.confidence == 0.9
        assert provider.call_count == 1

    asyncio.run(scenario())


def test_generate_structured_repairs_an_intent_outside_the_closed_set():
    async def scenario():
        # "creat_task" is a plausible LLM typo — not in IntentDetectionResult's
        # Literal set, so it must be schema-repaired, not silently accepted.
        provider = _ScriptedProvider([
            '{"intent": "creat_task", "confidence": 0.8}',
            '{"intent": "create_task", "confidence": 0.8}',
        ])
        gateway = LLMGateway(provider)
        result = await gateway.generate_structured(user_prompt="hi", schema=IntentDetectionResult)
        assert result.intent == "create_task"
        assert provider.call_count == 2, "must have used exactly the one repair retry"

    asyncio.run(scenario())


def test_generate_structured_repairs_out_of_range_confidence():
    async def scenario():
        provider = _ScriptedProvider([
            '{"intent": "delete_task", "confidence": 5.0}',  # out of [0, 1]
            '{"intent": "delete_task", "confidence": 1.0}',
        ])
        gateway = LLMGateway(provider)
        result = await gateway.generate_structured(user_prompt="hi", schema=IntentDetectionResult)
        assert result.confidence == 1.0
        assert provider.call_count == 2

    asyncio.run(scenario())


def test_generate_structured_defaults_missing_confidence_without_a_retry():
    async def scenario():
        provider = _ScriptedProvider(['{"intent": "general"}'])  # confidence omitted entirely
        gateway = LLMGateway(provider)
        result = await gateway.generate_structured(user_prompt="hi", schema=IntentDetectionResult)
        assert result.confidence == 0.0, "missing confidence must default to 0.0 (uncertain), never 1.0"
        assert provider.call_count == 1, "a merely-omitted optional field must not burn a repair retry"

    asyncio.run(scenario())


def test_generate_structured_raises_schema_error_when_never_valid():
    async def scenario():
        provider = _ScriptedProvider([
            '{"intent": "not_a_real_intent", "confidence": 0.5}',
            '{"intent": "still_not_real", "confidence": 0.5}',
        ])
        gateway = LLMGateway(provider)
        raised = False
        try:
            await gateway.generate_structured(user_prompt="hi", schema=IntentDetectionResult)
        except LLMSchemaError:
            raised = True
        assert raised, "an intent that never lands in the closed set must never be silently returned"
        assert provider.call_count == 2

    asyncio.run(scenario())


def test_generate_structured_validates_db_query_sub_intent_against_the_closed_set():
    async def scenario():
        provider = _ScriptedProvider([
            '{"sub_intent": "user_cnt"}',  # typo — not a real sub_intent
            '{"sub_intent": "user_count"}',
        ])
        gateway = LLMGateway(provider)
        result = await gateway.generate_structured(user_prompt="how many users", schema=DbQueryExtraction)
        assert result.sub_intent == "user_count"
        assert provider.call_count == 2

    asyncio.run(scenario())


def test_generate_structured_available_on_base_provider_without_a_gateway():
    async def scenario():
        provider = _ScriptedProvider([
            '{"intent": "bogus_intent", "confidence": 0.5}',
            '{"intent": "general", "confidence": 0.5}',
        ])
        result = await provider.generate_structured(user_prompt="hi", schema=IntentDetectionResult)
        assert result.intent == "general"
        assert provider.call_count == 2

    asyncio.run(scenario())
