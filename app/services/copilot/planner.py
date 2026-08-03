"""Structured Planner (spec Section 20, bounded) — splits a single chat
message that contains multiple distinct goals ("create a task for the
launch and tell me how many tasks are overdue") into an ordered list of
independent, self-contained sub-instructions, each routed through the
existing single-intent pipeline and executed in sequence. Only engages when
the message actually looks like it might contain more than one request —
the common single-goal case never pays for the extra LLM call, and any
planner failure just falls back to treating the message as one step."""

import json
import logging

logger = logging.getLogger("copilot.planner")

_CONJUNCTION_MARKERS = (" and also", " also ", " and then", "; ", " as well as", " plus ")

_PLAN_SYSTEM = """The user's message may contain more than one distinct
request (e.g. "create a task for the launch and tell me how many tasks are
overdue"). Split it into an ordered list of independent, self-contained
sub-instructions — each rewritten so it makes sense completely on its own,
repeating any shared context (dates, names) in each step.

If the message is really just ONE request, return a list with that single
item, unchanged.

Return ONLY JSON: {"steps": ["first self-contained instruction", "second self-contained instruction"]}"""


def _looks_multi_goal(message: str) -> bool:
    lower = f" {message.lower()} "
    return any(marker in lower for marker in _CONJUNCTION_MARKERS)


async def maybe_split_goals(llm, message: str) -> list[str]:
    if not _looks_multi_goal(message):
        return [message]
    try:
        result = await llm.generate_text(
            system_prompt=_PLAN_SYSTEM,
            user_prompt=message,
            temperature=0.0,
            response_format="json",
        )
        text = result.text.strip().strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
        data = json.loads(text)
        steps = [s.strip() for s in data.get("steps", []) if isinstance(s, str) and s.strip()]
        return steps or [message]
    except Exception:
        logger.info("Goal splitting skipped (non-critical) — treating as one step", exc_info=True)
        return [message]
