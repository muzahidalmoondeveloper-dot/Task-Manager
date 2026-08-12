"""Evaluation Harness (spec Sections 50-51, bounded) — runs a hand-written
golden set of (message, expected_intent) pairs through the real intent
classifier and records one pass/fail row per case in ai_evaluation_results,
so regressions in classification accuracy are visible over time instead of
only caught by accident."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.copilot import AIEvaluationResult

GOLDEN_SET_INTENT_CLASSIFICATION: list[dict] = [
    {"message": "create a task to fix the login bug due tomorrow", "expected_intent": "create_task"},
    {"message": "show me my tasks", "expected_intent": "list_tasks"},
    {"message": "mark task 12 as done", "expected_intent": "update_task"},
    {"message": "delete task 7", "expected_intent": "delete_task"},
    {"message": "delete all tasks", "expected_intent": "delete_task"},
    {"message": "how many users are there", "expected_intent": "db_query"},
    {"message": "how many tasks are overdue", "expected_intent": "db_query"},
    {"message": "what's a good way to prioritize my week", "expected_intent": "general"},
    {"message": "convert request 3 into a task", "expected_intent": "convert_request_to_task"},
    {"message": "show our KPIs", "expected_intent": "db_query"},
]

# Bengali (Bangla script) and Banglish (Latin-script transliteration)
# variants of a representative subset of the English cases above
# (architecture item 7/17 — "Bengali/Banglish evaluation dataset"). Each
# pair is a genuine translation, not a copy-pasted English string, so this
# actually exercises the LLM's multilingual understanding (per the note
# added to _INTENT_SYSTEM/_DB_QUERY_EXTRACT_SYSTEM this same pass), not just
# the deterministic ordinal/deictic reference layer covered by
# tests/test_bengali_banglish_reference_understanding.py.
GOLDEN_SET_INTENT_CLASSIFICATION_BENGALI: list[dict] = [
    {"message": "লগইন বাগ ঠিক করার জন্য আগামীকাল একটা টাস্ক তৈরি করো", "expected_intent": "create_task"},  # Bangla script
    {"message": "amake task ta banai dao login bug thik korar jonno, deadline agamikal", "expected_intent": "create_task"},  # Banglish
    {"message": "আমার টাস্কগুলো দেখাও", "expected_intent": "list_tasks"},  # Bangla script
    {"message": "amar task gulo dekhao", "expected_intent": "list_tasks"},  # Banglish
    {"message": "১২ নম্বর টাস্কটা done হিসেবে মার্ক করো", "expected_intent": "update_task"},  # Bangla script
    {"message": "task 12 ta done kore dao", "expected_intent": "update_task"},  # Banglish
    {"message": "৭ নম্বর টাস্কটা মুছে ফেলো", "expected_intent": "delete_task"},  # Bangla script
    {"message": "task 7 ta delete kore dao", "expected_intent": "delete_task"},  # Banglish
    {"message": "কতজন ইউজার আছে", "expected_intent": "db_query"},  # Bangla script — "how many users are there"
    {"message": "koyta task overdue ache", "expected_intent": "db_query"},  # Banglish — "how many tasks are overdue"
]


async def run_intent_classification_suite(
    db: AsyncSession, chat_service, suite_name: str = "intent_classification",
    golden_set: list[dict] | None = None,
) -> list[dict]:
    results = []
    for case in (golden_set if golden_set is not None else GOLDEN_SET_INTENT_CLASSIFICATION):
        intent, confidence = await chat_service._detect_intent(case["message"], "")
        passed = intent == case["expected_intent"]
        results.append({
            "message": case["message"], "expected_intent": case["expected_intent"],
            "actual_intent": intent, "confidence": confidence, "passed": passed,
        })
        db.add(AIEvaluationResult(
            suite=suite_name,
            case_name=case["message"][:255],
            expected_json={"intent": case["expected_intent"]},
            actual_json={"intent": intent, "confidence": confidence},
            passed=passed,
            notes="",
        ))
    await db.commit()
    return results


async def run_bengali_evaluation_suite(db: AsyncSession, chat_service) -> list[dict]:
    """Architecture item 17 — Bengali/Banglish evaluation dataset. Uses a
    real LLM call per case (unlike the deterministic reference-resolution
    tests), so — unlike the rest of this session's regression suite — this
    requires a live LLM_PROVIDER to actually be configured and reachable;
    it's an evaluation harness (spec Sections 50-51), not a unit/live-DB
    regression test, and is invoked on demand, not part of `pytest`'s
    always-run suite."""
    return await run_intent_classification_suite(
        db, chat_service, suite_name="intent_classification_bengali",
        golden_set=GOLDEN_SET_INTENT_CLASSIFICATION_BENGALI,
    )
