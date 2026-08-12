import logging
from datetime import date

from pydantic import ValidationError

from app.services.llm.gateway import LLMSchemaError

from app.schemas.task_suggestion import (
    CONFIDENCE_VALUES,
    EmailAnalysisResult,
    ExtractedTask,
)
from app.services.llm import get_llm_provider

logger = logging.getLogger("ai_task_extractor")


TASK_EXTRACTION_FORMAT = {
    "type": "object",
    "properties": {
        "should_create_tasks": {"type": "boolean"},
        "reason": {"type": "string"},
        "source_category": {
            "type": "string",
            "enum": [
                "work_action",
                "meeting_followup",
                "client_request",
                "internal_update",
                "newsletter",
                "promotion",
                "advertisement",
                "spam",
                "receipt",
                "notification",
                "personal",
                "unknown",
            ],
        },
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": ["string", "null"]},
                    "suggested_start_date": {"type": ["string", "null"]},
                    "suggested_due_date": {"type": ["string", "null"]},
                    "suggested_assignee_name": {"type": ["string", "null"]},
                    "suggested_assignee_email": {"type": ["string", "null"]},
                    "suggested_project_name": {"type": ["string", "null"]},
                    "suggested_team_name": {"type": ["string", "null"]},
                    "confidence": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                    },
                },
                "required": [
                    "title",
                    "description",
                    "suggested_start_date",
                    "suggested_due_date",
                    "suggested_assignee_name",
                    "suggested_assignee_email",
                    "suggested_project_name",
                    "suggested_team_name",
                    "confidence",
                ],
            },
        },
    },
    "required": [
        "should_create_tasks",
        "reason",
        "source_category",
        "tasks",
    ],
}

_SYSTEM_PROMPT_BASE = (
    "You are an email and meeting transcript task extraction engine for a task management app. "
    "First decide whether this source should create tasks. "
    "Return should_create_tasks=false for spam, advertisements, promotions, newsletters, marketing emails, "
    "discount offers, generic notifications, receipts without follow-up action, thank-you messages, and FYI-only messages. "
    "Only return should_create_tasks=true when there is a clear work-related action item, request, commitment, assignment, deliverable, or follow-up. "
    "Even if assignee, project, team, start date, or due date is unknown, still return the task if the action item is clear. Use null for unknown fields. "
    "Do not create tasks from vague informational messages. "
    "Do not invent assignees, dates, projects, teams, or emails. "
    "If unknown, use null. "
    "Dates must be YYYY-MM-DD. "
    "Every task object must include title, description, suggested_start_date, suggested_due_date, "
    "suggested_assignee_name, suggested_assignee_email, suggested_project_name, suggested_team_name, and confidence. "
    'confidence must be exactly one of the strings "low", "medium", or "high" — never a number. '
    "Return only JSON matching the required schema."
)

_USERS_BLOCK_HEADER = (
    "\n\nKnown system users — when assigning tasks, use ONLY these exact names and emails. "
    "Match people mentioned in the source text to this list by name similarity:\n"
)


def _normalize_confidence(raw: object) -> str:
    if raw is None:
        return "medium"
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in CONFIDENCE_VALUES:
            return lowered
        try:
            raw = float(lowered)
        except ValueError:
            return "medium"
    if isinstance(raw, bool):
        return "high" if raw else "low"
    if isinstance(raw, (int, float)):
        x = float(raw)
        if x > 1.0:
            x = min(x / 100.0, 1.0)
        if x >= 0.67:
            return "high"
        if x >= 0.34:
            return "medium"
        return "low"
    return "medium"


class AITaskExtractor:
    async def extract_tasks(
        self,
        *,
        source_type: str,
        source_title: str | None,
        source_text: str,
        known_users: list[dict] | None = None,
    ) -> tuple[list[ExtractedTask], dict]:
        provider = get_llm_provider()

        today_str = date.today().isoformat()
        system_prompt = (
            _SYSTEM_PROMPT_BASE
            + f"\n\nToday's date is {today_str}. "
            "All suggested start and due dates must be on or after today unless the source text explicitly states a past date."
        )
        if known_users:
            users_lines = "\n".join(
                f"- {u['name']} <{u['email']}>" for u in known_users
            )
            system_prompt = system_prompt + _USERS_BLOCK_HEADER + users_lines

        user_prompt = (
            f"Source type: {source_type}\n"
            f"Source title: {source_title or 'Untitled'}\n\n"
            f"Content:\n{source_text}"
        )

        # generate_json() (architecture item 4/5 — Structured LLM Gateway):
        # was raw generate_text() + a bespoke _parse_json() with NO repair
        # retry at all — a single malformed response from the LLM raised
        # ValueError immediately and failed the whole email/transcript
        # import. generate_json() gets the same repair-retry/fallback
        # machinery as every other structured extraction call site in the
        # app, while json_schema still gets the native-format-hint benefit
        # (Ollama's `format=`) this call site relied on.
        try:
            payload = await provider.generate_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.1,
                json_schema=TASK_EXTRACTION_FORMAT,
                capability="email_task_extraction",
            )
        except LLMSchemaError as exc:
            logger.warning("Task extraction gave up after retries/fallback: %s", exc)
            return [], {"should_create_tasks": False, "reason": "AI extraction failed", "source_category": "unknown", "tasks": []}

        payload = self._normalize_payload(payload)
        logger.info("Parsed LLM JSON: %s", payload)

        if payload.get("should_create_tasks") is False:
            logger.info(
                "Skipping source. source_category=%s reason=%s",
                payload.get("source_category"),
                payload.get("reason"),
            )
            return [], payload

        valid_tasks: list[ExtractedTask] = []

        for item in payload.get("tasks", []):
            if not isinstance(item, dict):
                continue

            if "title" not in item:
                if "name" in item:
                    item["title"] = item["name"]
                elif "task" in item:
                    item["title"] = item["task"]
                elif "description" in item and item["description"]:
                    item["title"] = str(item["description"])[:120]
                else:
                    continue

            if "suggested_start_date" not in item:
                item["suggested_start_date"] = item.get("start_date")

            if "suggested_due_date" not in item:
                item["suggested_due_date"] = item.get("due_date")

            item.setdefault("description", None)
            item.setdefault("suggested_start_date", None)
            item.setdefault("suggested_due_date", None)
            item.setdefault("suggested_assignee_name", None)
            item.setdefault("suggested_assignee_email", None)
            item.setdefault("suggested_project_name", None)
            item.setdefault("suggested_team_name", None)

            item["confidence"] = _normalize_confidence(item.get("confidence"))

            try:
                valid_tasks.append(ExtractedTask(**item))
            except ValidationError as exc:
                logger.warning("Skipping invalid extracted task: %s", item)
                logger.warning("Validation error: %s", exc)

        payload["tasks"] = [task.model_dump(mode="json") for task in valid_tasks]

        try:
            EmailAnalysisResult(
                should_create_tasks=payload.get("should_create_tasks", bool(valid_tasks)),
                reason=payload.get("reason", ""),
                source_category=payload.get("source_category", "unknown"),
                tasks=valid_tasks,
            )
        except ValidationError as exc:
            logger.warning("EmailAnalysisResult validation warning: %s", exc)

        return valid_tasks, payload

    def _normalize_payload(self, payload) -> dict:
        """generate_json() already parsed the raw JSON text (with repair-
        retry on failure) — this only normalizes the shape, same as the
        old _parse_json() did after its own (now-replaced) manual
        json.loads() call: a bare JSON array is treated as a task list, and
        a missing "tasks" key defaults to empty rather than KeyError-ing
        every call site below."""
        if isinstance(payload, list):
            payload = {"tasks": payload}

        if not isinstance(payload, dict):
            raise ValueError("LLM response must be a JSON object or an array of tasks.")

        if "tasks" not in payload:
            payload["tasks"] = []

        return payload
