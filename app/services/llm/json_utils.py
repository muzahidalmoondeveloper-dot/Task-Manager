"""Shared JSON-from-LLM parsing (architecture Section 37/5 — Structured LLM
Gateway). Lives here (not in chat_service.py) so both chat_service.py and
gateway.py can import the same canonical parser without a circular import
(chat_service imports the llm package; the llm package must not import back)."""

import json


def parse_llm_json(text: str) -> dict:
    """Strip markdown code-fence wrapping an LLM sometimes adds around JSON
    output, then parse. Raises json.JSONDecodeError (a ValueError subclass)
    on anything that isn't valid JSON — callers decide whether to retry,
    repair, fall back, or surface a user-facing error; this function never
    silently returns a partial/guessed result."""
    cleaned = text.strip()
    for prefix in ("```json", "```"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3].strip()
    return json.loads(cleaned)
