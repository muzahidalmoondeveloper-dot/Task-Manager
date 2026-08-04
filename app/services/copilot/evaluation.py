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


async def run_intent_classification_suite(
    db: AsyncSession, chat_service, suite_name: str = "intent_classification",
) -> list[dict]:
    results = []
    for case in GOLDEN_SET_INTENT_CLASSIFICATION:
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
