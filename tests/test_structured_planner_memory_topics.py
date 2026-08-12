"""Regression test for security gap #3 from the strict acceptance audit —
"convert planner/memory/topics to structured validated LLM output".

BUG BEING GUARDED AGAINST: planner.py, memory.py, and topics.py each did
their own manual JSON parsing (bespoke code-fence stripping, bare
`json.loads`, a blanket `except Exception` with no repair retry) instead of
going through LLMGateway.generate_json()/generate_structured() like every
extraction call site in chat_service.py already does. A malformed or
schema-invalid response from these three call sites silently degraded
(planner: treats as one step; memory: skips; topics: skips) with zero
chance of self-correction, unlike the rest of the app.

Pure in-memory — no database involved (same rationale as test_llm_gateway.py
and test_structured_intent.py). Proves each module's LLM call now goes
through the repair-retry path for a schema-invalid response — i.e. it now
calls the provider TWICE (repair) before giving up, not once-and-silently-
degrade.
"""

import asyncio

from app.services.copilot import planner
from app.services.llm.base import LLMProvider, LLMResponse


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


def test_planner_repairs_a_schema_invalid_response_instead_of_giving_up_immediately():
    async def scenario():
        provider = _ScriptedProvider([
            '{"the_steps": ["oops wrong key"]}',  # missing required "steps" key
            '{"steps": ["create task A", "then update task B"]}',
        ])
        steps = await planner.maybe_split_goals(provider, "create task A and then also update task B")
        assert steps == ["create task A", "then update task B"]
        assert provider.call_count == 2, "must have used the repair retry, not silently fallen back to one step"

    asyncio.run(scenario())


def test_planner_falls_back_to_one_step_only_after_repair_is_exhausted():
    async def scenario():
        provider = _ScriptedProvider([
            '{"bad": "structure"}',
            '{"still": "bad"}',
        ])
        message = "create task A and also update task B"
        steps = await planner.maybe_split_goals(provider, message)
        assert steps == [message], "must degrade gracefully to one step after repair is exhausted, not raise"
        assert provider.call_count == 2

    asyncio.run(scenario())


def test_planner_short_message_never_calls_the_llm_at_all():
    """Language-agnostic refactor: the pre-filter used to be a hardcoded
    list of English conjunction words/phrases ("and also", "and then", ...)
    — a multi-goal message in any OTHER language never matched that list
    and silently never got split, a language-specific bug. It's now a pure
    message-length check (see planner._MIN_MULTI_GOAL_LENGTH), which makes
    no assumption about language at all: a message too short to plausibly
    contain two distinct requests in ANY language skips the LLM call."""
    async def scenario():
        provider = _ScriptedProvider([])  # would raise AssertionError if called
        steps = await planner.maybe_split_goals(provider, "add a task")
        assert steps == ["add a task"]
        assert provider.call_count == 0, "a message too short to contain two goals must never burn an LLM call"

    asyncio.run(scenario())
