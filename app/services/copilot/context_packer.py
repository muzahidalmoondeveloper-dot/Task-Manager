"""Context Packer + Conflict Resolver (spec Sections 18-19, bounded) —
assembles the non-DB context (topic summary, saved preferences, raw
history) prepended to LLM prompts, under a size budget, explicitly labeled
as advisory. This is the mechanism behind "DB wins over memory": rather
than diffing individual facts (which would need a structured
entity-tracking system this app doesn't have), every handler that answers
questions about current system state already queries the DB fresh each
time (see chat_service.py's db_query handlers) — this packer just makes
sure the LLM is never told to treat topic/memory text as equally
authoritative to a live tool result in the same prompt."""

CONFLICT_RULE = (
    "If anything below (topic summary, saved preferences, prior conversation) "
    "conflicts with live data returned by a tool or database lookup elsewhere "
    "in this prompt, the live data is always correct — the context below may "
    "be stale and is for conversational continuity only."
)


def pack_context(
    *,
    topic_summary: str = "",
    saved_memories: list[tuple[str, str]] | None = None,
    history: str = "",
    max_chars: int = 4000,
) -> str:
    parts: list[str] = []
    if topic_summary:
        parts.append(f"Current topic summary (advisory, may be outdated): {topic_summary}")
    if saved_memories:
        prefs = "; ".join(f"{k}={v}" for k, v in saved_memories)
        parts.append(f"Saved user preferences: {prefs}")
    if history:
        parts.append(f"Recent conversation:\n{history}")
    if not parts:
        return ""

    body = "\n\n".join(parts)
    if len(body) > max_chars:
        # Keep the most recent content (end of the string) — the oldest
        # history is the least relevant to the current turn.
        body = body[-max_chars:]
    return f"{CONFLICT_RULE}\n\n{body}"
