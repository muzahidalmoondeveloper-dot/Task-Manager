"""Contextual Query Rewriter (spec Section 12) — expands a follow-up message
that depends on prior conversational context ("assign it to her", "same for
the other one", "what about tomorrow?") into a self-contained, explicit
instruction before intent detection and sub-intent extraction run on it.
Skips the LLM call entirely for messages that already look self-contained,
so it adds no latency/cost to the common case."""

import logging

logger = logging.getLogger("copilot.query_rewriter")

_PRONOUN_MARKERS = {
    "it", "that", "this", "them", "those", "these", "he", "she", "him", "her",
    "same", "again", "also", "too", "one", "ones",
}

_REWRITE_SYSTEM = """You rewrite a user's follow-up chat message into a fully
self-contained instruction, using the conversation history to fill in
anything implicit (pronouns, omitted entities, elided verbs).

Rules:
- Preserve the user's intent exactly — do not add, remove, or guess new
  requirements that weren't implied by the history.
- If the message is already self-contained, or if the history doesn't
  provide enough context to safely resolve a reference, return it
  completely unchanged.
- Never invent specific names, IDs, or values that aren't in the history.

Return ONLY the rewritten message text, nothing else — no quotes, no JSON, no explanation."""


def _looks_context_dependent(message: str) -> bool:
    words = {w.strip(".,!?").lower() for w in message.split()}
    return bool(words & _PRONOUN_MARKERS) or len(words) <= 3


async def rewrite_query(llm, message: str, history: str) -> str:
    """Best-effort — any failure, or a lack of history to resolve against,
    returns the original message unchanged rather than risking a bad rewrite
    silently corrupting what the user actually asked for."""
    if not history.strip() or not _looks_context_dependent(message):
        return message
    try:
        result = await llm.generate_text(
            system_prompt=_REWRITE_SYSTEM,
            user_prompt=f"Conversation history:\n{history}\n\nFollow-up message: {message}",
            temperature=0.0,
            capability="query_rewrite",
        )
        rewritten = result.text.strip().strip('"')
        return rewritten if rewritten else message
    except Exception:
        logger.info("Query rewrite skipped (non-critical)", exc_info=True)
        return message
