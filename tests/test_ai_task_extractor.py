"""ai_task_extractor.py regression test (item 4 — "Fix ai_task_extractor.py
to use the structured LLM gateway instead of raw JSON parsing").

BUG BEING GUARDED AGAINST: the old `_parse_json()` did a bespoke
code-fence-strip + `json.loads()` with NO repair retry — a single
malformed response from the LLM raised `ValueError` immediately and failed
the entire email/transcript import, unlike every other extraction call
site in the app (which all go through generate_json()/generate_structured()
with a repair-retry). This test proves extract_tasks() now survives one
malformed response (repairs and succeeds) and degrades gracefully — not
with a raised exception — when repair is exhausted, and that the
json_schema hint is still forwarded to the provider (native-format-hint
behavior preserved, not lost in the migration to generate_json()).

Pure in-memory — no database involved.
"""

import asyncio
import json

from app.services.ai_task_extractor import AITaskExtractor, TASK_EXTRACTION_FORMAT
from app.services.llm.base import LLMProvider, LLMResponse


_VALID_PAYLOAD = json.dumps({
    "should_create_tasks": True,
    "reason": "clear action item",
    "source_category": "work_action",
    "tasks": [{
        "title": "Fix the login bug",
        "description": None,
        "suggested_start_date": None,
        "suggested_due_date": None,
        "suggested_assignee_name": None,
        "suggested_assignee_email": None,
        "suggested_project_name": None,
        "suggested_team_name": None,
        "confidence": "high",
    }],
})


class _ScriptedProvider(LLMProvider):
    def __init__(self, replies: list[str]):
        self._replies = list(replies)
        self.call_count = 0
        self.last_json_schema = None

    async def generate_text(self, **kwargs):
        self.last_json_schema = kwargs.get("json_schema")
        if self.call_count >= len(self._replies):
            raise AssertionError(f"_ScriptedProvider called more times than scripted ({self.call_count + 1})")
        reply = self._replies[self.call_count]
        self.call_count += 1
        return LLMResponse(text=reply)


def test_extract_tasks_repairs_one_malformed_response(monkeypatch):
    provider = _ScriptedProvider(["not valid json at all", _VALID_PAYLOAD])
    monkeypatch.setattr("app.services.ai_task_extractor.get_llm_provider", lambda: provider)

    extractor = AITaskExtractor()
    tasks, payload = asyncio.run(extractor.extract_tasks(source_type="email", source_title="Test", source_text="please fix the login bug"))

    assert len(tasks) == 1 and tasks[0].title == "Fix the login bug"
    assert provider.call_count == 2, "must have used the repair retry, not raised on the first malformed response"
    # The native structured-output hint must still be forwarded to the provider.
    assert provider.last_json_schema == TASK_EXTRACTION_FORMAT


def test_extract_tasks_degrades_gracefully_when_repair_is_exhausted(monkeypatch):
    provider = _ScriptedProvider(["still not json", "still not json either"])
    monkeypatch.setattr("app.services.ai_task_extractor.get_llm_provider", lambda: provider)

    extractor = AITaskExtractor()
    tasks, payload = asyncio.run(extractor.extract_tasks(source_type="email", source_title="Test", source_text="whatever"))

    assert tasks == [], "must degrade to no tasks, not raise, when the LLM never returns parseable JSON"
    assert payload["should_create_tasks"] is False
    assert provider.call_count == 2


def test_extract_tasks_normalizes_a_bare_json_array_response(monkeypatch):
    array_payload = json.dumps([{
        "title": "Array-shaped task",
        "description": None, "suggested_start_date": None, "suggested_due_date": None,
        "suggested_assignee_name": None, "suggested_assignee_email": None,
        "suggested_project_name": None, "suggested_team_name": None, "confidence": "medium",
    }])
    provider = _ScriptedProvider([array_payload])
    monkeypatch.setattr("app.services.ai_task_extractor.get_llm_provider", lambda: provider)

    extractor = AITaskExtractor()
    tasks, payload = asyncio.run(extractor.extract_tasks(source_type="email", source_title="Test", source_text="x"))

    assert len(tasks) == 1 and tasks[0].title == "Array-shaped task"
