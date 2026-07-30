"""Unit tests for the centralized KPI logic (app/services/kpi_service.py)."""

from dataclasses import dataclass, field
from datetime import date

import pytest

from app.services.kpi_service import (
    compute_derived_entries,
    compute_view_statuses,
    derive_entries,
    evaluate_status,
    interpolate,
    period_key,
    validate_entry_value,
    validate_kpi_config,
)


@dataclass
class Entry:
    period_start: date
    period_type: str
    value: float | None


@dataclass
class FakeKpi:
    formula: str | None = None
    target_type: str = "number"
    reference_value: float | None = None
    reference_max: float | None = None
    interpolation: str = "no_interpolation"
    supported_views: list = field(default_factory=lambda: ["weekly", "monthly"])
    is_snoozed: bool = False
    snoozed_until: date | None = None
    entries: list = field(default_factory=list)


# ─── Period math ──────────────────────────────────────────────────────────────

class TestPeriodKey:
    def test_weekly_monday(self):
        assert period_key(date(2026, 7, 15), "weekly") == date(2026, 7, 13)  # Wed → Mon

    def test_weekly_sunday_belongs_to_previous_monday(self):
        assert period_key(date(2026, 7, 19), "weekly") == date(2026, 7, 13)

    def test_weekly_monday_is_itself(self):
        assert period_key(date(2026, 7, 13), "weekly") == date(2026, 7, 13)

    def test_monthly(self):
        assert period_key(date(2026, 7, 31), "monthly") == date(2026, 7, 1)

    def test_quarterly_boundaries(self):
        assert period_key(date(2026, 1, 1), "quarterly") == date(2026, 1, 1)
        assert period_key(date(2026, 3, 31), "quarterly") == date(2026, 1, 1)
        assert period_key(date(2026, 4, 1), "quarterly") == date(2026, 4, 1)
        assert period_key(date(2026, 12, 31), "quarterly") == date(2026, 10, 1)

    def test_yearly(self):
        assert period_key(date(2026, 12, 31), "yearly") == date(2026, 1, 1)

    def test_unknown_view_raises(self):
        with pytest.raises(ValueError):
            period_key(date(2026, 1, 1), "daily")


# ─── Interpolation ────────────────────────────────────────────────────────────

class TestInterpolate:
    def test_latest_value(self):
        assert interpolate([10, 20, 30, 40], "latest_value") == 40

    def test_cumulative(self):
        assert interpolate([10, 20, 30, 40], "cumulative") == 100

    def test_average(self):
        assert interpolate([70, 80, 90, 100], "average") == 85

    def test_zero_is_a_valid_value(self):
        # Zeros participate: they are recorded values, not missing ones.
        assert interpolate([0, 10], "average") == 5
        assert interpolate([10, 0], "latest_value") == 0
        assert interpolate([0, 0], "cumulative") == 0

    def test_empty_returns_none(self):
        assert interpolate([], "average") is None

    def test_no_interpolation_returns_none(self):
        assert interpolate([1, 2], "no_interpolation") is None


class TestDeriveEntries:
    def _weeklies(self):
        # Four weeks of July 2026 (Mondays: 6, 13, 20, 27)
        return [
            Entry(date(2026, 7, 6), "weekly", 10),
            Entry(date(2026, 7, 13), "weekly", 20),
            Entry(date(2026, 7, 20), "weekly", 30),
            Entry(date(2026, 7, 27), "weekly", 40),
        ]

    def test_weekly_to_monthly_cumulative(self):
        derived = derive_entries(self._weeklies(), "monthly", "cumulative")
        assert derived == [
            {"period_start": date(2026, 7, 1), "period_type": "monthly", "value": 100, "interpolated": True}
        ]

    def test_weekly_to_monthly_latest(self):
        derived = derive_entries(self._weeklies(), "monthly", "latest_value")
        assert derived[0]["value"] == 40

    def test_weekly_to_monthly_average(self):
        derived = derive_entries(self._weeklies(), "monthly", "average")
        assert derived[0]["value"] == 25

    def test_manual_monthly_value_wins(self):
        entries = self._weeklies() + [Entry(date(2026, 7, 1), "monthly", 999)]
        derived = derive_entries(entries, "monthly", "cumulative")
        assert derived == []  # the manual entry suppresses the derived one

    def test_missing_values_are_skipped_but_zero_counts(self):
        entries = [
            Entry(date(2026, 7, 6), "weekly", None),   # missing → skipped
            Entry(date(2026, 7, 13), "weekly", 0),     # zero → counted
            Entry(date(2026, 7, 20), "weekly", 30),
        ]
        derived = derive_entries(entries, "monthly", "average")
        assert derived[0]["value"] == 15  # (0 + 30) / 2

    def test_no_interpolation_derives_nothing(self):
        assert derive_entries(self._weeklies(), "monthly", "no_interpolation") == []

    def test_uses_finest_available_granularity(self):
        # Weekly data exists → quarterly must aggregate weeks, not months.
        entries = self._weeklies() + [Entry(date(2026, 8, 1), "monthly", 7)]
        derived = derive_entries(entries, "quarterly", "cumulative")
        assert derived == [
            {"period_start": date(2026, 7, 1), "period_type": "quarterly", "value": 100, "interpolated": True}
        ]


# ─── Status evaluation ────────────────────────────────────────────────────────

def _status(formula, value, ref, *, target_type="number", ref_max=None, prev=None, **kw):
    return evaluate_status(
        formula=formula,
        target_type=target_type,
        reference_value=ref,
        reference_max=ref_max,
        value=value,
        previous_value=prev,
        **kw,
    )


class TestEvaluateStatus:
    # gte: target >= 100, 10% tolerance → 90..99.99 at risk
    def test_gte(self):
        assert _status("gte", 100, 100) == "on_track"
        assert _status("gte", 150, 100) == "on_track"
        assert _status("gte", 95, 100) == "at_risk"
        assert _status("gte", 90, 100) == "at_risk"     # boundary of the band
        assert _status("gte", 89.9, 100) == "off_track"

    # lte: target <= 10 → 10..11 at risk
    def test_lte(self):
        assert _status("lte", 10, 10) == "on_track"
        assert _status("lte", 3, 10) == "on_track"
        assert _status("lte", 10.5, 10) == "at_risk"
        assert _status("lte", 11.5, 10) == "off_track"

    def test_gt_and_lt_are_strict(self):
        assert _status("gt", 100, 100) == "at_risk"     # not strictly greater
        assert _status("gt", 100.1, 100) == "on_track"
        assert _status("lt", 5, 5) == "at_risk"
        assert _status("lt", 4.9, 5) == "on_track"

    def test_equals_is_binary(self):
        assert _status("equals", 5, 5) == "on_track"
        assert _status("equals", 5.000000001, 5) == "on_track"  # float tolerance
        assert _status("equals", 4, 5) == "off_track"           # no at-risk band

    def test_between(self):
        # 70..85, band = 15 * 0.10 = 1.5
        assert _status("between", 70, 70, ref_max=85) == "on_track"
        assert _status("between", 85, 70, ref_max=85) == "on_track"
        assert _status("between", 69, 70, ref_max=85) == "at_risk"
        assert _status("between", 86, 70, ref_max=85) == "at_risk"
        assert _status("between", 60, 70, ref_max=85) == "off_track"
        assert _status("between", 95, 70, ref_max=85) == "off_track"

    def test_boolean_binary(self):
        assert _status("equals", 1, 1, target_type="boolean") == "on_track"
        assert _status("equals", 0, 1, target_type="boolean") == "off_track"

    def test_time_uses_numeric_comparison(self):
        # Response time <= 10 minutes
        assert _status("lte", 9, 10, target_type="time") == "on_track"
        assert _status("lte", 10.8, 10, target_type="time") == "at_risk"
        assert _status("lte", 20, 10, target_type="time") == "off_track"

    def test_direction(self):
        # gte = should rise or hold
        assert _status("gte", 12, None, target_type="direction", prev=10) == "on_track"
        assert _status("gte", 10, None, target_type="direction", prev=10) == "on_track"
        assert _status("gte", 8, None, target_type="direction", prev=10) == "off_track"
        # lt = should strictly fall
        assert _status("lt", 8, None, target_type="direction", prev=10) == "on_track"
        assert _status("lt", 10, None, target_type="direction", prev=10) == "off_track"
        # equals = should hold steady
        assert _status("equals", 10, None, target_type="direction", prev=10) == "on_track"
        assert _status("equals", 11, None, target_type="direction", prev=10) == "off_track"
        # first data point has no previous value → nothing to evaluate
        assert _status("gte", 10, None, target_type="direction", prev=None) == "no_data"

    def test_no_data_cases(self):
        assert _status("gte", None, 100) == "no_data"
        assert _status(None, 50, None) == "no_data"       # no formula configured
        assert _status("gte", 50, None) == "no_data"      # no reference configured

    def test_snooze_silences_healthy_kpis(self):
        assert _status("gte", 200, 100, is_snoozed=True) == "snoozed"   # on track underneath
        assert _status("gte", None, 100, is_snoozed=True) == "snoozed"  # no data underneath

    def test_snooze_breaks_when_at_risk_or_off_track(self):
        # "Snooze until status becomes at-risk or off-track."
        assert _status("gte", 95, 100, is_snoozed=True) == "at_risk"
        assert _status("gte", 50, 100, is_snoozed=True) == "off_track"

    def test_snooze_expires(self):
        today = date(2026, 7, 18)
        assert _status("gte", 200, 100, is_snoozed=True,
                       snoozed_until=date(2026, 7, 1), today=today) == "on_track"
        assert _status("gte", 200, 100, is_snoozed=True,
                       snoozed_until=date(2026, 8, 1), today=today) == "snoozed"

    def test_zero_value_is_evaluated_not_treated_as_missing(self):
        assert _status("lte", 0, 5) == "on_track"
        assert _status("gte", 0, 100) == "off_track"


class TestComputeViewStatuses:
    def test_latest_recorded_value_drives_status(self):
        kpi = FakeKpi(
            formula="gte", reference_value=100,
            entries=[
                Entry(date(2026, 7, 6), "weekly", 200),
                Entry(date(2026, 7, 13), "weekly", 50),   # latest weekly → off track
                Entry(date(2026, 7, 1), "monthly", 150),  # monthly → on track
            ],
        )
        statuses = compute_view_statuses(kpi, today=date(2026, 7, 18))
        assert statuses["weekly"] == "off_track"
        assert statuses["monthly"] == "on_track"

    def test_only_supported_views_are_reported(self):
        kpi = FakeKpi(supported_views=["weekly"])
        statuses = compute_view_statuses(kpi)
        assert set(statuses) == {"weekly"}
        assert statuses["weekly"] == "no_data"

    def test_derived_entries_never_drive_status(self):
        # Weekly values interpolate into monthly, but the monthly status must
        # come only from manual monthly entries (here: none → no_data).
        kpi = FakeKpi(
            formula="gte", reference_value=10, interpolation="cumulative",
            entries=[Entry(date(2026, 7, 6), "weekly", 50)],
        )
        statuses = compute_view_statuses(kpi, today=date(2026, 7, 18))
        assert statuses["monthly"] == "no_data"
        derived = compute_derived_entries(kpi)
        assert any(d["period_type"] == "monthly" and d["value"] == 50 for d in derived)


# ─── Config validation ────────────────────────────────────────────────────────

class TestValidateKpiConfig:
    def _ok(self, **overrides):
        base = dict(
            interpolation="no_interpolation", target_type="number",
            formula=None, reference_value=None, reference_max=None,
            supported_views=["weekly"],
        )
        base.update(overrides)
        validate_kpi_config(**base)

    def test_valid_config_passes(self):
        self._ok(formula="gte", reference_value=100)

    def test_bad_enums_rejected(self):
        for bad in (
            {"interpolation": "sum"},
            {"target_type": "money"},
            {"formula": "<="},
            {"supported_views": ["daily"]},
        ):
            with pytest.raises(ValueError):
                self._ok(**bad)

    def test_empty_views_rejected(self):
        with pytest.raises(ValueError):
            self._ok(supported_views=[])

    def test_formula_requires_reference(self):
        with pytest.raises(ValueError):
            self._ok(formula="gte")

    def test_direction_needs_no_reference(self):
        self._ok(target_type="direction", formula="gte")

    def test_between_requires_max(self):
        with pytest.raises(ValueError):
            self._ok(formula="between", reference_value=70)

    def test_between_min_greater_than_max_rejected(self):
        with pytest.raises(ValueError):
            self._ok(formula="between", reference_value=85, reference_max=70)

    def test_between_valid(self):
        self._ok(formula="between", reference_value=70, reference_max=85)

    def test_reference_max_only_with_between(self):
        with pytest.raises(ValueError):
            self._ok(formula="gte", reference_value=1, reference_max=5)

    def test_boolean_only_equals(self):
        with pytest.raises(ValueError):
            self._ok(target_type="boolean", formula="gte", reference_value=1)
        self._ok(target_type="boolean", formula="equals", reference_value=1)

    def test_boolean_reference_must_be_0_or_1(self):
        with pytest.raises(ValueError):
            self._ok(target_type="boolean", formula="equals", reference_value=2)

    def test_direction_between_rejected(self):
        with pytest.raises(ValueError):
            self._ok(target_type="direction", formula="between", reference_max=5)


class TestValidateEntryValue:
    def test_boolean_accepts_only_0_1_or_none(self):
        validate_entry_value("boolean", 0.0)
        validate_entry_value("boolean", 1.0)
        validate_entry_value("boolean", None)
        with pytest.raises(ValueError):
            validate_entry_value("boolean", 2.0)

    def test_time_rejects_negative(self):
        validate_entry_value("time", 0.0)
        with pytest.raises(ValueError):
            validate_entry_value("time", -1.0)

    def test_non_finite_rejected(self):
        with pytest.raises(ValueError):
            validate_entry_value("number", float("inf"))
        with pytest.raises(ValueError):
            validate_entry_value("number", float("nan"))


# ─── API schema wiring ────────────────────────────────────────────────────────

class TestSchemaValidation:
    def test_kpi_create_rejects_between_without_max(self):
        from pydantic import ValidationError
        from app.schemas.kpi import KPICreate
        with pytest.raises(ValidationError):
            KPICreate(title="t", rock_id=1, formula="between", reference_value=70)

    def test_kpi_create_valid(self):
        from app.schemas.kpi import KPICreate
        k = KPICreate(title="t", rock_id=1, formula="between", reference_value=70, reference_max=85)
        assert k.reference_max == 85
