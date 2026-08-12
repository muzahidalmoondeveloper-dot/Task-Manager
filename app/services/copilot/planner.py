"""Structured Planner (spec Section 20, bounded) — splits a single chat
message that contains multiple distinct goals ("create a task for the
launch and tell me how many tasks are overdue") into an ordered list of
independent, self-contained sub-instructions, each routed through the
existing single-intent pipeline and executed in sequence. Only engages when
the message is long enough to plausibly contain more than one request — a
short message can't, in any language, so the common single-goal case still
never pays for the extra LLM call; any planner failure falls back to
treating the message as one step.

Language-agnostic refactor: this used to pre-filter on a hardcoded list of
English conjunction words/phrases ("and also", "and then", ...) before even
attempting the LLM split — meaning a multi-goal message written in any
other language (whose equivalent conjunctions were never in that list)
silently never got split, degrading non-English users specifically. The
length-only gate below carries no language assumption at all — it's a
cheap, purely structural proxy ("this is too short to contain two distinct
requests"), not a word-pattern match — so every language gets the same
LLM-driven split decision."""

import logging

from app.services.copilot.intent_schemas import GoalSplitResult

logger = logging.getLogger("copilot.planner")

# Below this length, a message is too short to plausibly contain two
# distinct goals in any language — skipping the LLM call here is a
# language-agnostic length check, not a word-pattern match.
_MIN_MULTI_GOAL_LENGTH = 20

_PLAN_SYSTEM = """The user's message may contain more than one distinct
request (e.g. "create a task for the launch and tell me how many tasks are
overdue"), possibly written in any language. Split it into an ordered list
of independent, self-contained sub-instructions — each rewritten so it
makes sense completely on its own, repeating any shared context (dates,
names) in each step, in the same language the user wrote in.

If the message is really just ONE request, return a list with that single
item, unchanged.

Return ONLY JSON: {"steps": ["first self-contained instruction", "second self-contained instruction"]}"""


def _looks_multi_goal(message: str) -> bool:
    return len(message.strip()) >= _MIN_MULTI_GOAL_LENGTH


async def maybe_split_goals(llm, message: str) -> list[str]:
    if not _looks_multi_goal(message):
        return [message]
    try:
        # generate_structured() (architecture item 4/5 — security gap #3
        # from the strict acceptance audit) — was raw generate_text() +
        # manual code-fence stripping + json.loads() with a bare
        # except-Exception fallback and no repair retry, unlike every
        # extraction call site in chat_service.py.
        result = await llm.generate_structured(
            system_prompt=_PLAN_SYSTEM,
            user_prompt=message,
            schema=GoalSplitResult,
            temperature=0.0,
            capability="planner",
        )
        steps = [s.strip() for s in result.steps if s.strip()]
        return steps or [message]
    except Exception:
        logger.info("Goal splitting skipped (non-critical) — treating as one step", exc_info=True)
        return [message]
