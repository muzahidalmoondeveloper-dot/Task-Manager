"""Unit tests for the AI Copilot's pure-logic modules (app/services/copilot/*
and app/services/llm/gateway.py). Follows this repo's existing test
convention (see test_kpi_service.py): synchronous, no DB fixture — modules
that need a live database (change_sets, transaction, approvals, memory,
topics, audit, proactive) were instead verified this session via live-DB
scripts against the real dev Postgres, since no async DB test fixture
exists in this codebase yet."""

import asyncio

from app.services.copilot import (
    context_packer,
    planner,
    policy,
    query_rewriter,
    reference_resolver,
    risk,
)
from app.services.copilot.reference_resolver import ResolutionStatus
from app.services.llm.base import LLMProvider, LLMResponse
from app.services.llm.gateway import CircuitBreaker, LLMGateway


def run(coro):
    return asyncio.run(coro)


# ─── risk.py ──────────────────────────────────────────────────────────────────

class TestRisk:
    def test_known_tools_map_to_expected_levels(self):
        assert risk.risk_level("create_task") == "R2"
        assert risk.risk_level("reassign_task") == "R3"
        assert risk.risk_level("update_task_bulk") == "R4"
        assert risk.risk_level("delete_task_bulk") == "R7"
        assert risk.risk_level("convert_client_request_to_task") == "R3"

    def test_unknown_tool_defaults_to_r2(self):
        assert risk.risk_level("some_new_tool") == "R2"

    def test_risk_action_gating(self):
        assert risk.risk_action("create_task") == risk.AUTO
        assert risk.risk_action("reassign_task") == risk.CONFIRM
        assert risk.risk_action("delete_task_bulk") == risk.BLOCK

    def test_requires_admin_approval_only_for_team_manager_bulk_update(self):
        assert risk.requires_admin_approval("update_task_bulk", "team_manager") is True
        assert risk.requires_admin_approval("update_task_bulk", "owner") is False
        assert risk.requires_admin_approval("update_task_bulk", "admin") is False
        assert risk.requires_admin_approval("reassign_task", "team_manager") is False


# ─── policy.py ────────────────────────────────────────────────────────────────

class TestPolicy:
    def test_client_blocked_sub_intents(self):
        assert policy.is_client_blocked_sub_intent("user_list") is True
        assert policy.is_client_blocked_sub_intent("kpi_list") is True
        assert policy.is_client_blocked_sub_intent("meeting_upcoming") is True

    def test_client_request_list_not_blocked_for_clients(self):
        assert policy.is_client_blocked_sub_intent("client_request_list") is False

    def test_always_blocked_tools_denied_for_any_role(self):
        assert policy.check_tool_policy(org_role="owner", tool_name="change_user_role") == policy.DENY
        assert policy.check_tool_policy(org_role="admin", tool_name="bulk_delete_all_tasks") == policy.DENY

    def test_client_denied_all_write_tools(self):
        assert policy.check_tool_policy(org_role="client", tool_name="reassign_task") == policy.DENY

    def test_staff_allowed_ordinary_write_tools(self):
        assert policy.check_tool_policy(org_role="team_manager", tool_name="reassign_task") == policy.ALLOW


# ─── reference_resolver.py ────────────────────────────────────────────────────

class _Task:
    def __init__(self, id, name):
        self.id = id
        self.name = name


class _User:
    def __init__(self, id, full_name):
        self.id = id
        self.full_name = full_name


class TestResolveByName:
    def test_exact_word_resolves(self):
        users = [_User(1, "Sarah Connor"), _User(2, "John Doe")]
        result = reference_resolver.resolve_by_name(users, "John", lambda u: u.full_name, lambda u: u.id)
        assert result.status == ResolutionStatus.RESOLVED
        assert result.entity.id == 2

    def test_ambiguous_when_two_close_matches(self):
        users = [_User(1, "Sarah Connor"), _User(2, "Sarah Smith")]
        result = reference_resolver.resolve_by_name(users, "Sarah", lambda u: u.full_name, lambda u: u.id)
        assert result.status == ResolutionStatus.AMBIGUOUS
        assert len(result.candidates) == 2

    def test_not_found_for_unrelated_query(self):
        users = [_User(1, "Sarah Connor"), _User(2, "John Doe")]
        result = reference_resolver.resolve_by_name(users, "Zorblatt", lambda u: u.full_name, lambda u: u.id)
        assert result.status == ResolutionStatus.NOT_FOUND

    def test_empty_ref_not_found(self):
        result = reference_resolver.resolve_by_name([_User(1, "A")], "", lambda u: u.full_name, lambda u: u.id)
        assert result.status == ResolutionStatus.NOT_FOUND

    def test_clarification_message_for_custom_label(self):
        users = [_User(1, "Sarah Connor"), _User(2, "Sarah Smith")]
        result = reference_resolver.resolve_by_name(users, "Sarah", lambda u: u.full_name, lambda u: u.id)
        msg = result.clarification_message_for("person")
        assert "person" in msg


class TestResolveTaskReference:
    def test_numeric_ref_matches_by_id(self):
        tasks = [_Task(5, "Fix login bug"), _Task(6, "Write docs")]
        result = run(reference_resolver.resolve_task_reference(None, tasks, "5"))
        assert result.status == ResolutionStatus.RESOLVED
        assert result.entity.id == 5

    def test_numeric_ref_not_found(self):
        tasks = [_Task(5, "Fix login bug")]
        result = run(reference_resolver.resolve_task_reference(None, tasks, "999"))
        assert result.status == ResolutionStatus.NOT_FOUND

    def test_empty_candidates_not_found(self):
        result = run(reference_resolver.resolve_task_reference(None, [], "anything"))
        assert result.status == ResolutionStatus.NOT_FOUND


# ─── query_rewriter.py ────────────────────────────────────────────────────────

class _FakeLLM(LLMProvider):
    # Subclasses the real LLMProvider ABC (see test_chat_service_safety.py's
    # matching comment) so it gets generate_json()/generate_structured()'s
    # base-class defaults for free — planner.py now calls generate_structured(),
    # and a fake that only implements generate_text() would raise
    # AttributeError instead of exercising the code path this test verifies.
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    async def generate_text(self, **kwargs):
        self.calls += 1
        return LLMResponse(text=self.reply)


class TestQueryRewriter:
    def test_skips_when_no_history(self):
        llm = _FakeLLM("should not be used")
        out = run(query_rewriter.rewrite_query(llm, "assign it to her", ""))
        assert out == "assign it to her"
        assert llm.calls == 0

    def test_skips_self_contained_message(self):
        llm = _FakeLLM("should not be used")
        out = run(query_rewriter.rewrite_query(llm, "show me all overdue tasks for finance team", "some history"))
        assert llm.calls == 0
        assert out == "show me all overdue tasks for finance team"

    def test_rewrites_context_dependent_message(self):
        llm = _FakeLLM("Assign task 42 to Sarah")
        out = run(query_rewriter.rewrite_query(llm, "assign it to her", "task 42 exists, Sarah is on the team"))
        assert llm.calls == 1
        assert out == "Assign task 42 to Sarah"


# ─── planner.py ───────────────────────────────────────────────────────────────

class TestPlanner:
    def test_single_goal_skips_llm_call(self):
        llm = _FakeLLM("should not be used")
        steps = run(planner.maybe_split_goals(llm, "show me my tasks"))
        assert steps == ["show me my tasks"]
        assert llm.calls == 0

    def test_multi_goal_marker_triggers_split(self):
        llm = _FakeLLM('{"steps": ["do the first thing", "do the second thing"]}')
        steps = run(planner.maybe_split_goals(llm, "do the first thing and also do the second thing"))
        assert llm.calls == 1
        assert steps == ["do the first thing", "do the second thing"]

    def test_llm_failure_falls_back_to_single_step(self):
        class BrokenLLM:
            async def generate_text(self, **kwargs):
                raise RuntimeError("boom")
        steps = run(planner.maybe_split_goals(BrokenLLM(), "a and also b"))
        assert steps == ["a and also b"]


# ─── context_packer.py ────────────────────────────────────────────────────────

class TestContextPacker:
    def test_empty_inputs_produce_empty_string(self):
        assert context_packer.pack_context() == ""

    def test_includes_conflict_rule_when_any_content_present(self):
        packed = context_packer.pack_context(history="User: hi")
        assert "live data" in packed.lower()
        assert "User: hi" in packed

    def test_saved_memories_rendered(self):
        packed = context_packer.pack_context(saved_memories=[("report_format", "PDF")])
        assert "report_format=PDF" in packed

    def test_truncates_to_max_chars_keeping_the_tail(self):
        long_history = "x" * 5000
        packed = context_packer.pack_context(history=long_history, max_chars=100)
        assert len(packed) <= 100 + len(context_packer.CONFLICT_RULE) + 2
        assert packed.endswith("x" * 10)  # tail preserved, not the head


# ─── llm/gateway.py ───────────────────────────────────────────────────────────

class _FailingProvider:
    def __init__(self):
        self.calls = 0

    async def generate_text(self, **kwargs):
        self.calls += 1
        raise RuntimeError("simulated outage")


class _WorkingProvider:
    def __init__(self):
        self.calls = 0

    async def generate_text(self, **kwargs):
        self.calls += 1
        return LLMResponse(text="fallback response")


class TestCircuitBreaker:
    def test_starts_closed(self):
        assert CircuitBreaker().is_open is False

    def test_opens_after_threshold_failures(self):
        cb = CircuitBreaker(failure_threshold=2)
        cb.record_failure()
        assert cb.is_open is False
        cb.record_failure()
        assert cb.is_open is True

    def test_success_resets_failure_count(self):
        cb = CircuitBreaker(failure_threshold=2)
        cb.record_failure()
        cb.record_success()
        cb.record_failure()
        assert cb.is_open is False  # only 1 consecutive failure since reset


class TestLLMGateway:
    def test_no_fallback_always_calls_primary(self):
        primary = _WorkingProvider()
        gw = LLMGateway(primary, fallback=None)
        result = run(gw.generate_text(user_prompt="hi"))
        assert result.text == "fallback response"
        assert primary.calls == 1

    def test_falls_back_when_primary_fails(self):
        primary = _FailingProvider()
        fallback = _WorkingProvider()
        gw = LLMGateway(primary, fallback)
        result = run(gw.generate_text(user_prompt="hi"))
        assert result.text == "fallback response"
        assert primary.calls == 1
        assert fallback.calls == 1

    def test_circuit_opens_and_skips_primary(self):
        primary = _FailingProvider()
        fallback = _WorkingProvider()
        gw = LLMGateway(primary, fallback)
        gw._breaker._failure_threshold = 1
        run(gw.generate_text(user_prompt="hi"))
        assert gw._breaker.is_open is True
        run(gw.generate_text(user_prompt="hi"))
        assert primary.calls == 1  # not called again while circuit is open
        assert fallback.calls == 2
