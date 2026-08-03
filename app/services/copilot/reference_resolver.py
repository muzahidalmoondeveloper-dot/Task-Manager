"""Reference Resolver (spec Section 11) — turns a free-text reference like
"that task", "the marketing one", or "task 42" into a concrete,
confidence-scored match instead of silently guessing. An ambiguous result is
returned as such so the caller can ask a clarifying question rather than
acting on the first (possibly wrong) match — this is the concrete mechanism
behind "do not execute ambiguous writes".

Uses Postgres trigram similarity (pg_trgm) as the closest available
approximation to semantic matching in this environment: pgvector's binary
extension isn't installed here (verified via a direct
`CREATE EXTENSION vector` attempt, which fails with
FeatureNotSupportedError — it requires an OS-level compiled install this
Windows Postgres 18 instance doesn't have), while pg_trgm ships with
standard Postgres and was confirmed available by successfully running
`CREATE EXTENSION IF NOT EXISTS pg_trgm`.
"""

import difflib
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, TypeVar

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.task import Task

T = TypeVar("T")

# Score gaps below this between the top match and the runner-up mean the
# match isn't confident enough to act on automatically.
_CONFIDENT_MARGIN = 0.15
_MIN_SCORE = 0.25


class ResolutionStatus(str, Enum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"


@dataclass
class Candidate:
    id: int
    label: str
    score: float


@dataclass
class ResolutionResult:
    status: ResolutionStatus
    entity: Task | None = None
    candidates: list[Candidate] = field(default_factory=list)

    def clarification_message_for(self, entity_label: str = "task") -> str:
        if self.status == ResolutionStatus.NOT_FOUND:
            return f"I couldn't find a {entity_label} matching that. Could you give me the exact name or ID?"
        options = "\n".join(f'- "{c.label}"' for c in self.candidates[:5])
        return f"I found more than one {entity_label} that could match — which one did you mean?\n{options}"

    @property
    def clarification_message(self) -> str:
        return self.clarification_message_for("task")


async def resolve_task_reference(
    db: AsyncSession, candidate_tasks: list[Task], ref: str,
) -> ResolutionResult:
    """Resolve `ref` against `candidate_tasks` — the caller must pre-scope
    that list to whatever the requesting user/role is allowed to see; this
    function never queries beyond the given candidates."""
    ref = (ref or "").strip()
    if not ref:
        return ResolutionResult(ResolutionStatus.NOT_FOUND)

    if ref.isdigit():
        match = next((t for t in candidate_tasks if t.id == int(ref)), None)
        if match:
            return ResolutionResult(ResolutionStatus.RESOLVED, entity=match)
        return ResolutionResult(ResolutionStatus.NOT_FOUND)

    if not candidate_tasks:
        return ResolutionResult(ResolutionStatus.NOT_FOUND)

    task_ids = [t.id for t in candidate_tasks]
    by_id = {t.id: t for t in candidate_tasks}
    result = await db.execute(
        select(Task.id, func.similarity(Task.name, ref).label("score"))
        .where(Task.id.in_(task_ids))
    )
    scored = [Candidate(id=row.id, label=by_id[row.id].name, score=float(row.score or 0)) for row in result.all()]

    # A plain substring hit is a strong signal trigram similarity alone can
    # under-score for short queries against long task names — floor it up.
    ref_lower = ref.lower()
    for c in scored:
        if ref_lower in by_id[c.id].name.lower():
            c.score = max(c.score, 0.5)
    scored.sort(key=lambda c: c.score, reverse=True)

    viable = [c for c in scored if c.score >= _MIN_SCORE]
    if not viable:
        return ResolutionResult(ResolutionStatus.NOT_FOUND, candidates=scored[:5])

    if len(viable) == 1 or (viable[0].score - viable[1].score) >= _CONFIDENT_MARGIN:
        return ResolutionResult(ResolutionStatus.RESOLVED, entity=by_id[viable[0].id], candidates=viable[:5])

    return ResolutionResult(ResolutionStatus.AMBIGUOUS, candidates=viable[:5])


def resolve_by_name(
    items: list[T], ref: str, name_fn: Callable[[T], str], id_fn: Callable[[T], object],
) -> ResolutionResult:
    """In-memory fuzzy + keyword fusion resolver for small, already-loaded
    entity lists (users/projects/teams — typically a few dozen per org, so a
    DB round trip per lookup isn't worth it, unlike tasks). Combines a plain
    substring check with difflib's sequence-similarity ratio, then applies
    the same confident-margin ambiguity rule as resolve_task_reference."""
    ref = (ref or "").strip()
    if not ref or not items:
        return ResolutionResult(ResolutionStatus.NOT_FOUND)

    # difflib's whole-string ratio runs hot on short names (e.g. "zorblatt"
    # vs "sarah smith" scores 0.32 purely from shared letters) — a plain
    # trigram-style score doesn't behave like Postgres similarity() here, so
    # this resolver needs its own, higher floor rather than sharing
    # _MIN_SCORE with the task/pg_trgm path.
    _name_min_score = 0.55
    ref_lower = ref.lower()
    scored: list[tuple[T, float]] = []
    for item in items:
        name = name_fn(item)
        name_lower = name.lower()
        score = difflib.SequenceMatcher(None, ref_lower, name_lower).ratio()
        if ref_lower in name_lower:
            score = max(score, 0.7)
        scored.append((item, score))
    scored.sort(key=lambda pair: pair[1], reverse=True)

    viable = [pair for pair in scored if pair[1] >= _name_min_score]
    if not viable:
        return ResolutionResult(ResolutionStatus.NOT_FOUND)

    if len(viable) == 1 or (viable[0][1] - viable[1][1]) >= _CONFIDENT_MARGIN:
        return ResolutionResult(ResolutionStatus.RESOLVED, entity=viable[0][0])

    return ResolutionResult(
        ResolutionStatus.AMBIGUOUS,
        candidates=[Candidate(id=id_fn(i), label=name_fn(i), score=s) for i, s in viable[:5]],
    )
