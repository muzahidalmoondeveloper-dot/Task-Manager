"""Pure, DB-independent calculations for the Project Report Module.

These functions take plain ORM objects or dicts and return numbers/strings —
no I/O — so they can be unit-tested without a database and reused by both
``ReportGenerationService`` (building snapshots) and the PDF template context.
"""

from __future__ import annotations

from datetime import date

from app.services import kpi_service

ROCK_COMPLETE_STATUSES = {"complete"}
ROCK_OFF_TRACK_STATUSES = {"off_track", "canceled"}
ROCK_ON_TRACK_STATUSES = {"on_track", "planned", "backlog"}

MILESTONE_COMPLETE_STATUSES = {"complete"}

TASK_DONE_STATUSES = {"done"}
TASK_BLOCKED_STATUSES = {"blocked"}

# Weighting per spec §21. Budget is out of scope for v1, so its weight is
# folded proportionally into the remaining four categories.
_WEIGHTS = {"rock": 0.30, "kpi": 0.25, "task": 0.20, "timeline": 0.15}
_WEIGHT_TOTAL = sum(_WEIGHTS.values())


def rock_progress_pct(rock) -> int:
    """Percentage of a Rock's milestones marked complete."""
    milestones = list(rock.milestones or [])
    if not milestones:
        return 100 if rock.status in ROCK_COMPLETE_STATUSES else 0
    completed = sum(1 for m in milestones if m.status in MILESTONE_COMPLETE_STATUSES)
    return round(completed / len(milestones) * 100)


def rock_score(rocks: list) -> float:
    """0-100 aggregate: average completion percentage across all Rocks."""
    if not rocks:
        return 0.0
    return sum(rock_progress_pct(r) for r in rocks) / len(rocks)


def kpi_status(kpi) -> str:
    """Reuse the app's single source of truth for KPI status evaluation."""
    recorded = sorted(
        (e for e in (kpi.entries or []) if e.value is not None),
        key=lambda e: e.period_start,
    )
    latest = recorded[-1].value if recorded else None
    previous = recorded[-2].value if len(recorded) > 1 else None
    return kpi_service.evaluate_status(
        formula=kpi.formula,
        target_type=kpi.target_type,
        reference_value=kpi.reference_value,
        reference_max=kpi.reference_max,
        value=latest,
        previous_value=previous,
        is_snoozed=kpi.is_snoozed,
        snoozed_until=kpi.snoozed_until,
    )


def kpi_latest_value(kpi) -> float | None:
    recorded = sorted(
        (e for e in (kpi.entries or []) if e.value is not None),
        key=lambda e: e.period_start,
    )
    return recorded[-1].value if recorded else None


def kpi_previous_value(kpi) -> float | None:
    recorded = sorted(
        (e for e in (kpi.entries or []) if e.value is not None),
        key=lambda e: e.period_start,
    )
    return recorded[-2].value if len(recorded) > 1 else None


def kpi_trend(kpi) -> str:
    latest = kpi_latest_value(kpi)
    previous = kpi_previous_value(kpi)
    if latest is None or previous is None:
        return "stable"
    if latest > previous:
        return "improving"
    if latest < previous:
        return "declining"
    return "stable"


def kpi_score(kpis: list) -> float:
    """0-100 aggregate: percentage of KPIs currently on_track."""
    if not kpis:
        return 0.0
    on_track = sum(1 for k in kpis if kpi_status(k) == kpi_service.STATUS_ON_TRACK)
    return on_track / len(kpis) * 100


def task_score(tasks: list) -> float:
    """0-100 aggregate: percentage of tasks completed."""
    if not tasks:
        return 0.0
    done = sum(1 for t in tasks if t.status in TASK_DONE_STATUSES)
    return done / len(tasks) * 100


def milestone_delay_days(milestone, today: date | None = None) -> int | None:
    """Positive = late. Uses actual_end_date if the milestone is complete,
    otherwise the forecast_end_date, per spec §14."""
    today = today or date.today()
    planned = getattr(milestone, "planned_end_date", None) or milestone.due_date
    if planned is None:
        return None
    if milestone.status in MILESTONE_COMPLETE_STATUSES:
        actual = getattr(milestone, "actual_end_date", None)
        if actual is None:
            return None
        return (actual - planned).days
    forecast = getattr(milestone, "forecast_end_date", None)
    if forecast is None:
        return None
    return (forecast - planned).days


def timeline_score(milestones: list) -> float:
    """0-100 aggregate: percentage of milestones on time (delay <= 0), of
    those with enough date data to evaluate. No data → neutral 100."""
    evaluable = [m for m in milestones if milestone_delay_days(m) is not None]
    if not evaluable:
        return 100.0
    on_time = sum(1 for m in evaluable if milestone_delay_days(m) <= 0)
    return on_time / len(evaluable) * 100


def weighted_health_score(*, rock: float, kpi: float, task: float, timeline: float) -> float:
    """Combine four 0-100 component scores per spec §21's weighting,
    renormalized since Budget is out of scope for v1."""
    scores = {"rock": rock, "kpi": kpi, "task": task, "timeline": timeline}
    weighted = sum(scores[key] * _WEIGHTS[key] for key in _WEIGHTS)
    return round(weighted / _WEIGHT_TOTAL, 1)


def overall_health_score(*, rocks: list, kpis: list, tasks: list, milestones: list) -> float:
    """Weighted overall health score per spec §21, computed from live data."""
    return weighted_health_score(
        rock=rock_score(rocks),
        kpi=kpi_score(kpis),
        task=task_score(tasks),
        timeline=timeline_score(milestones),
    )


def health_rating(score: float) -> str:
    if score >= 90:
        return "excellent"
    if score >= 75:
        return "good"
    if score >= 60:
        return "needs_attention"
    return "critical"


def health_status(score: float) -> str:
    if score >= 75:
        return "on_track"
    if score >= 60:
        return "at_risk"
    return "off_track"


def dashboard_summary(report) -> dict:
    """Read-only Executive Dashboard numbers computed from a report's *own*
    snapshots (immutable once finalized), not live project data. Shared by
    the API's dashboard payload and the PDF template context."""
    rocks = report.rock_snapshots
    kpis = report.kpi_snapshots
    tasks = report.task_snapshots
    milestones = report.milestone_snapshots

    rock_avg = sum(r.progress_pct for r in rocks) / len(rocks) if rocks else 0.0
    kpi_on_track = sum(1 for k in kpis if k.status == "on_track")
    kpi_pct = (kpi_on_track / len(kpis) * 100) if kpis else 0.0
    tasks_done = sum(1 for t in tasks if t.status in TASK_DONE_STATUSES)
    task_pct = (tasks_done / len(tasks) * 100) if tasks else 0.0
    milestones_done = sum(1 for m in milestones if m.status in MILESTONE_COMPLETE_STATUSES)
    on_time_milestones = [m for m in milestones if m.delay_days is not None]
    timeline_pct = (
        sum(1 for m in on_time_milestones if m.delay_days <= 0) / len(on_time_milestones) * 100
        if on_time_milestones else 100.0
    )
    score = weighted_health_score(rock=rock_avg, kpi=kpi_pct, task=task_pct, timeline=timeline_pct)

    return {
        "overall_progress": round(rock_avg, 1),
        "health_score": score,
        "health_rating": health_rating(score),
        "health_status": health_status(score),
        "rock_progress_pct": round(rock_avg, 1),
        "kpi_performance_pct": round(kpi_pct, 1),
        "tasks_completed": tasks_done,
        "tasks_total": len(tasks),
        "milestones_completed": milestones_done,
        "milestones_total": len(milestones),
        "open_risks": sum(1 for r in report.risk_snapshots if r.status != "closed"),
        "blocked_tasks": sum(1 for t in tasks if t.is_blocked),
    }
