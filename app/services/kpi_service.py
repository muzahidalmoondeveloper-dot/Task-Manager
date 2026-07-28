"""Centralized KPI business logic: status evaluation, interpolation, period math.

This module is the single source of truth for KPI calculations. The frontend
only *renders* what the API returns — it never re-derives statuses or
interpolated values.

Definitions used throughout:

- A "view" is one of: weekly, monthly, quarterly, yearly.
- A period key is the ISO date of the period's first day
  (Monday / 1st of month / quarter start / Jan 1).
- Missing values: entries whose ``value`` is None are treated as "not
  recorded" and are skipped by aggregation. A recorded ``0`` is a valid
  value and participates in every calculation.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

VIEWS = ["weekly", "monthly", "quarterly", "yearly"]
# Ordering used to decide which granularity feeds interpolation.
_VIEW_RANK = {"weekly": 0, "monthly": 1, "quarterly": 2, "yearly": 3}

INTERPOLATIONS = {"no_interpolation", "latest_value", "cumulative", "average"}
TARGET_TYPES = {"number", "currency", "percentage", "boolean", "direction", "time"}
FORMULAS = {"lte", "gte", "lt", "gt", "between", "equals"}

STATUS_NO_DATA = "no_data"
STATUS_ON_TRACK = "on_track"
STATUS_AT_RISK = "at_risk"
STATUS_OFF_TRACK = "off_track"
STATUS_SNOOZED = "snoozed"

# "At risk" band: how far past the target a value may drift (relative to the
# target magnitude) before it is off-track. E.g. target >= 100 with 0.10
# tolerance → 90..99.99 is at risk, below 90 is off track.
AT_RISK_TOLERANCE = 0.10
# Relative tolerance for float equality (equals formula, boolean compare).
_EQ_RTOL = 1e-9
_EQ_ATOL = 1e-9


# ─── Validation ───────────────────────────────────────────────────────────────

def validate_kpi_config(
    *,
    interpolation: str | None,
    target_type: str | None,
    formula: str | None,
    reference_value: float | None,
    reference_max: float | None,
    supported_views: list | None,
) -> None:
    """Raise ValueError when the KPI configuration is inconsistent."""
    if interpolation is not None and interpolation not in INTERPOLATIONS:
        raise ValueError(f"interpolation must be one of: {', '.join(sorted(INTERPOLATIONS))}")
    if target_type is not None and target_type not in TARGET_TYPES:
        raise ValueError(f"target_type must be one of: {', '.join(sorted(TARGET_TYPES))}")
    if formula is not None and formula not in FORMULAS:
        raise ValueError(f"formula must be one of: {', '.join(sorted(FORMULAS))}")
    if supported_views is not None:
        if not supported_views:
            raise ValueError("At least one supported view is required.")
        invalid = [v for v in supported_views if v not in VIEWS]
        if invalid:
            raise ValueError(f"Unsupported views: {', '.join(map(str, invalid))}")

    # Direction compares consecutive values, so it needs no reference at all.
    needs_reference = formula is not None and target_type != "direction"
    if needs_reference and reference_value is None:
        raise ValueError("A reference value is required when a formula is set.")
    if formula == "between":
        if target_type == "direction":
            raise ValueError("The 'between' formula is not supported for direction KPIs.")
        if reference_max is None:
            raise ValueError("The 'between' formula requires both a minimum and a maximum value.")
        if reference_value is not None and reference_value > reference_max:
            raise ValueError("Minimum reference value cannot be greater than the maximum.")
    if formula != "between" and reference_max is not None:
        raise ValueError("reference_max is only valid with the 'between' formula.")
    if target_type == "boolean":
        if formula not in (None, "equals"):
            raise ValueError("Boolean KPIs only support the 'equals' formula.")
        if reference_value is not None and reference_value not in (0.0, 1.0):
            raise ValueError("Boolean reference value must be 0 (No) or 1 (Yes).")


def validate_entry_value(target_type: str, value: float | None) -> None:
    """Raise ValueError for values that make no sense for the target type."""
    for v in (value,):
        if v is None:
            continue
        if not math.isfinite(v):
            raise ValueError("Value must be a finite number.")
        if target_type == "boolean" and v not in (0.0, 1.0):
            raise ValueError("Boolean KPI values must be 0 (No) or 1 (Yes).")
        if target_type == "time" and v < 0:
            raise ValueError("Time values cannot be negative.")


# ─── Period math ──────────────────────────────────────────────────────────────

def period_key(d: date, view: str) -> date:
    """First day of the period containing ``d`` for the given view."""
    if view == "weekly":
        return d - timedelta(days=d.weekday())  # Monday
    if view == "monthly":
        return d.replace(day=1)
    if view == "quarterly":
        return date(d.year, (d.month - 1) // 3 * 3 + 1, 1)
    if view == "yearly":
        return date(d.year, 1, 1)
    raise ValueError(f"Unknown view: {view}")


# ─── Interpolation ────────────────────────────────────────────────────────────

def interpolate(values: list[float], method: str) -> float | None:
    """Aggregate recorded values of one larger period. ``values`` must be in
    chronological order and contain only recorded (non-None) values —
    zeros included."""
    if not values:
        return None
    if method == "latest_value":
        return values[-1]
    if method == "cumulative":
        return sum(values)
    if method == "average":
        return sum(values) / len(values)
    return None  # no_interpolation


def derive_entries(entries: list, view: str, method: str) -> list[dict]:
    """Derive values for ``view`` periods from finer-grained recorded entries.

    ``entries`` are ORM/attr objects with .value, .period_start, .period_type.
    Only periods with no manually recorded entry for ``view`` are derived, so a
    manual value always wins. Uses the finest granularity that has data below
    the target view. Returns dicts flagged ``interpolated: True``.
    """
    if method == "no_interpolation" or method not in INTERPOLATIONS:
        return []

    target_rank = _VIEW_RANK[view]
    manual_keys = {e.period_start for e in entries if e.period_type == view}

    # Finest granularity below the target view that has recorded values.
    source_type = None
    for candidate in VIEWS:  # weekly first = finest
        if _VIEW_RANK[candidate] >= target_rank:
            break
        if any(e.period_type == candidate and e.value is not None for e in entries):
            source_type = candidate
            break
    if source_type is None:
        return []

    buckets: dict[date, list] = {}
    for e in entries:
        if e.period_type != source_type or e.value is None:
            continue
        key = period_key(e.period_start, view)
        if key in manual_keys:
            continue
        buckets.setdefault(key, []).append(e)

    derived = []
    for key, bucket in sorted(buckets.items()):
        bucket.sort(key=lambda e: e.period_start)
        value = interpolate([e.value for e in bucket], method)
        if value is not None:
            derived.append({
                "period_start": key,
                "period_type": view,
                "value": value,
                "interpolated": True,
            })
    return derived


# ─── Status evaluation ────────────────────────────────────────────────────────

def _passes(formula: str, value: float, ref: float, ref_max: float | None) -> bool:
    if formula == "lte":
        return value <= ref
    if formula == "gte":
        return value >= ref
    if formula == "lt":
        return value < ref
    if formula == "gt":
        return value > ref
    if formula == "equals":
        return math.isclose(value, ref, rel_tol=_EQ_RTOL, abs_tol=_EQ_ATOL)
    if formula == "between":
        return ref <= value <= (ref_max if ref_max is not None else ref)
    return True


def _at_risk_band(formula: str, ref: float, ref_max: float | None) -> float:
    """Absolute width of the at-risk band beyond the target boundary."""
    if formula == "between" and ref_max is not None:
        span = ref_max - ref
        base = span if span > 0 else max(abs(ref), abs(ref_max))
    else:
        base = abs(ref)
    return max(base, 1.0) * AT_RISK_TOLERANCE


def evaluate_status(
    *,
    formula: str | None,
    target_type: str,
    reference_value: float | None,
    reference_max: float | None,
    value: float | None,
    previous_value: float | None = None,
    is_snoozed: bool = False,
    snoozed_until: date | None = None,
    today: date | None = None,
) -> str:
    """Evaluate a KPI's status from its latest recorded value.

    Rules:
    - snooze silences a KPI only while it is healthy: if the underlying
      status is at_risk or off_track, the snooze breaks and the real status
      is returned ("snooze until status becomes at-risk or off-track");
      a snooze also expires once snoozed_until has passed;
    - no recorded value → no_data;
    - no formula/reference configured → no_data (nothing to evaluate against);
    - equals/boolean: exact match → on_track, else off_track (no at-risk band);
    - direction: compares value to previous_value; the formula encodes the
      desired movement (gte/gt = should rise or hold, lte/lt = should fall or
      hold, equals = should stay the same);
    - other formulas: pass → on_track; miss within AT_RISK_TOLERANCE of the
      boundary → at_risk; further out → off_track.
    """
    today = today or date.today()
    underlying = _evaluate_raw(
        formula=formula,
        target_type=target_type,
        reference_value=reference_value,
        reference_max=reference_max,
        value=value,
        previous_value=previous_value,
    )
    snooze_active = is_snoozed and (snoozed_until is None or snoozed_until >= today)
    if snooze_active and underlying not in (STATUS_AT_RISK, STATUS_OFF_TRACK):
        return STATUS_SNOOZED
    return underlying


def _evaluate_raw(
    *,
    formula: str | None,
    target_type: str,
    reference_value: float | None,
    reference_max: float | None,
    value: float | None,
    previous_value: float | None,
) -> str:
    """Status evaluation without the snooze rule applied."""
    if value is None:
        return STATUS_NO_DATA

    if target_type == "direction":
        if formula is None or previous_value is None:
            return STATUS_NO_DATA
        delta = value - previous_value
        if formula in ("gte", "gt"):
            ok = delta > 0 if formula == "gt" else delta >= 0
        elif formula in ("lte", "lt"):
            ok = delta < 0 if formula == "lt" else delta <= 0
        else:  # equals — should stay the same
            ok = math.isclose(delta, 0.0, rel_tol=_EQ_RTOL, abs_tol=_EQ_ATOL)
        return STATUS_ON_TRACK if ok else STATUS_OFF_TRACK

    if formula is None or reference_value is None:
        return STATUS_NO_DATA

    if _passes(formula, value, reference_value, reference_max):
        return STATUS_ON_TRACK

    # Equals and boolean are binary — either it matches or it does not.
    if formula == "equals" or target_type == "boolean":
        return STATUS_OFF_TRACK

    band = _at_risk_band(formula, reference_value, reference_max)
    if formula in ("gte", "gt"):
        distance = reference_value - value
    elif formula in ("lte", "lt"):
        distance = value - reference_value
    else:  # between
        low, high = reference_value, reference_max if reference_max is not None else reference_value
        distance = low - value if value < low else value - high

    return STATUS_AT_RISK if distance <= band else STATUS_OFF_TRACK


def compute_view_statuses(kpi, today: date | None = None) -> dict[str, str]:
    """Status per supported view, evaluated on the most recent recorded value
    (manual entries only — interpolated values never drive status)."""
    today = today or date.today()
    statuses: dict[str, str] = {}
    views = kpi.supported_views or VIEWS
    for view in views:
        if view not in VIEWS:
            continue
        recorded = sorted(
            (e for e in (kpi.entries or []) if e.period_type == view and e.value is not None),
            key=lambda e: e.period_start,
        )
        latest = recorded[-1].value if recorded else None
        previous = recorded[-2].value if len(recorded) > 1 else None
        statuses[view] = evaluate_status(
            formula=kpi.formula,
            target_type=kpi.target_type,
            reference_value=kpi.reference_value,
            reference_max=kpi.reference_max,
            value=latest,
            previous_value=previous,
            is_snoozed=kpi.is_snoozed,
            snoozed_until=kpi.snoozed_until,
            today=today,
        )
    return statuses


def compute_derived_entries(kpi) -> list[dict]:
    """Interpolated entries for every supported view above the finest one."""
    if kpi.interpolation == "no_interpolation":
        return []
    result: list[dict] = []
    for view in kpi.supported_views or VIEWS:
        if view == "weekly" or view not in VIEWS:
            continue
        result.extend(derive_entries(list(kpi.entries or []), view, kpi.interpolation))
    return result


