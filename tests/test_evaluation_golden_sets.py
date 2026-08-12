"""Sanity checks for the evaluation harness's golden sets (architecture item
17 — regression tests + Bengali/Banglish evaluation dataset).

Deliberately does NOT invoke run_intent_classification_suite()/
run_bengali_evaluation_suite() themselves — those make real LLM calls per
case and require a live, configured LLM_PROVIDER, making them an on-demand
evaluation harness (spec Sections 50-51), not part of the always-run,
fully-deterministic pytest suite the rest of this session's tests belong to.
This file instead verifies the golden sets' own structural integrity: every
`expected_intent` is a real, currently-valid intent value (so the golden
set can't silently drift out of sync with IntentDetectionResult's Literal
set), the Bengali/Banglish set actually contains non-ASCII/Bangla-script
content (not just English copy-pasted into a differently-named list), and
every case has a non-empty message.
"""

from app.services.copilot.evaluation import (
    GOLDEN_SET_INTENT_CLASSIFICATION,
    GOLDEN_SET_INTENT_CLASSIFICATION_BENGALI,
)
from app.services.copilot.intent_schemas import IntentDetectionResult

_VALID_INTENTS = set(IntentDetectionResult.model_fields["intent"].annotation.__args__)


def _assert_well_formed(golden_set: list[dict]) -> None:
    assert golden_set, "golden set must not be empty"
    for case in golden_set:
        assert case.get("message", "").strip(), f"case has an empty message: {case!r}"
        assert case.get("expected_intent") in _VALID_INTENTS, (
            f"golden set has drifted out of sync with IntentDetectionResult's Literal set: {case!r}"
        )


def test_english_golden_set_is_well_formed():
    _assert_well_formed(GOLDEN_SET_INTENT_CLASSIFICATION)


def test_bengali_golden_set_is_well_formed():
    _assert_well_formed(GOLDEN_SET_INTENT_CLASSIFICATION_BENGALI)


def test_bengali_golden_set_actually_contains_non_english_content():
    # Every case must be genuine Bengali/Banglish, not English text that
    # happened to get filed under the Bengali list — either real Bangla
    # script (non-ASCII) or a recognisable Banglish word that isn't just
    # the English sentence unchanged.
    has_bangla_script = any(
        any(ord(ch) > 127 for ch in case["message"]) for case in GOLDEN_SET_INTENT_CLASSIFICATION_BENGALI
    )
    assert has_bangla_script, "the Bengali/Banglish golden set must include at least some real Bangla-script cases"

    english_messages = {case["message"] for case in GOLDEN_SET_INTENT_CLASSIFICATION}
    for case in GOLDEN_SET_INTENT_CLASSIFICATION_BENGALI:
        assert case["message"] not in english_messages, (
            f"Bengali/Banglish case must not be a copy of an English one verbatim: {case['message']!r}"
        )


def test_bengali_golden_set_covers_the_same_intents_as_the_english_set():
    # Not necessarily every intent, but a meaningful, representative
    # overlap — this dataset is meant to prove multilingual understanding
    # of the SAME intent space, not a disjoint one.
    english_intents = {c["expected_intent"] for c in GOLDEN_SET_INTENT_CLASSIFICATION}
    bengali_intents = {c["expected_intent"] for c in GOLDEN_SET_INTENT_CLASSIFICATION_BENGALI}
    overlap = english_intents & bengali_intents
    assert len(overlap) >= 4, f"expected substantial intent overlap between the two golden sets, got: {overlap}"
