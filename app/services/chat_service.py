"""Chat service: intent detection + natural-language task management."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import asyncio

from app.core.database import AsyncSessionLocal
from app.models.issue import Issue
from app.models.kpi import KPI, KPIEntry
from app.models.meeting import Meeting
from app.models.notification import Notification
from app.models.rock import Rock
from app.models.task_request import TaskRequest
from app.models.user import User
from app.repositories.chat_repository import ChatRepository
from app.repositories.task_repository import TaskRepository
from app.repositories.user_repository import UserRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.team_repository import TeamRepository
from app.core.org_roles import TEAM_MEMBER
from app.schemas.chat import ChatAction, ChatMessageResponse
from app.schemas.task import TaskCreate, TaskUpdate
from app.services.copilot import (
    approvals, audit, change_sets, context_packer, memory, planner, policy, query_rewriter,
    reference_resolver, risk, topics,
)
from app.services.copilot.reference_resolver import ResolutionStatus
from app.services.llm import get_llm_provider
from app.services.background_email import bg_send_task_assigned

if TYPE_CHECKING:
    from app.models.chat import ChatMessage, ChatSession

logger = logging.getLogger("chat_service")

# ─── Intent types ────────────────────────────────────────────────────────────

INTENT_CREATE_TASK = "create_task"
INTENT_LIST_TASKS = "list_tasks"
INTENT_UPDATE_TASK = "update_task"
INTENT_DELETE_TASK = "delete_task"
INTENT_ANALYZE_TEXT = "analyze_text"
INTENT_DB_QUERY = "db_query"
INTENT_CONVERT_REQUEST = "convert_request_to_task"
INTENT_GENERAL = "general"

# ─── Status normalization ─────────────────────────────────────────────────────

_STATUS_SYNONYMS: dict[str, str] = {
    "done": "done",
    "completed": "done",
    "finished": "done",
    "complete": "done",
    "closed": "done",
    "todo": "todo",
    "to do": "todo",
    "pending": "todo",
    "not started": "todo",
    "not_started": "todo",
    "new": "todo",
    "open": "todo",
    "in progress": "in_progress",
    "in_progress": "in_progress",
    "inprogress": "in_progress",
    "ongoing": "in_progress",
    "working": "in_progress",
    "started": "in_progress",
    "wip": "in_progress",
    "pending review": "pending_review",
    "pending_review": "pending_review",
    "review": "pending_review",
    "in review": "pending_review",
    "in_review": "pending_review",
    "reviewing": "pending_review",
    "under review": "pending_review",
    "needs review": "pending_review",
}

_PROJECT_STATUS_SYNONYMS: dict[str, str] = {
    "active": "active",
    "running": "active",
    "ongoing": "active",
    "in progress": "active",
    "paused": "paused",
    "on hold": "paused",
    "hold": "paused",
    "suspended": "paused",
    "completed": "completed",
    "done": "completed",
    "finished": "completed",
    "closed": "completed",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "stopped": "cancelled",
    "abandoned": "cancelled",
}


# The CLIENT-visibility rule lives in copilot.policy — the single canonical
# source both db_query and the risk-gated write tools consult.


def _normalize_status(status: str | None) -> str | None:
    if not status:
        return None
    return _STATUS_SYNONYMS.get(status.lower().strip())


def _normalize_project_status(status: str | None) -> str | None:
    if not status:
        return None
    return _PROJECT_STATUS_SYNONYMS.get(status.lower().strip())


# ─── Prompts ──────────────────────────────────────────────────────────────────

_INTENT_SYSTEM = """You are an intent classifier for a task management assistant.
Classify the user's message into exactly ONE of these intents:
- create_task: user wants to create one or more tasks (including "create tasks from those", "add those as tasks", "create tasks from the action items above")
- list_tasks: user wants to see, find, or summarise their tasks (e.g. "how many tasks", "show my tasks", "list all tasks")
- update_task: user wants to change or update tasks — including bulk operations like "mark all tasks as done", "set everything to in_progress"
- delete_task: user wants to delete or remove tasks — including bulk operations like "delete all tasks", "remove all tasks", "delete all of task", "please delete all of task"
- analyze_text: user pasted an email, meeting transcript, or document IN THIS MESSAGE and wants tasks extracted, OR explicitly asks to summarise/analyse a document/transcript/email they shared earlier in this conversation (e.g. "summarise that email", "extract tasks from the transcript above"). Do NOT use this for a short question or command that merely mentions an app entity (tasks, Rocks, KPIs, projects, issues) — those are db_query or general, even if the wording contains "explain" or "summarise" (e.g. "explain my rocks", "summarise my week" are db_query, not analyze_text — there is no pasted document to extract from).
- db_query: user is asking an analytical or lookup question about data in the system — e.g. "how many users are there", "list all projects", "what tasks are overdue", "who is in team Alpha", "what is the progress of project X", "how many tasks are done", "show active projects", "what tasks does John have", "system statistics", "workload summary", "who are the admins", "how many teams", "tasks due this week", "list all issues", "show open issues", "what rocks does team X have", "show all rocks", "show our KPIs", "how is KPI X doing", "upcoming meetings", "list meetings", "client requests", "task requests from clients", "explain my rocks", "explain my tasks", "summarise my week"
- convert_request_to_task: user wants to turn a submitted client task request into a real task — e.g. "convert that request into a task", "approve the client's request and make it a task", "turn request 5 into a task"
- general: any other question, greeting, or request not covered above

IMPORTANT rules:
- "delete all", "delete all tasks", "delete all of task", "remove all" → delete_task
- "mark all as done", "set all to complete", "update all tasks" → update_task
- Always prefer db_query over general when the user asks about counts, lists, statistics, user names, roles, team members, project progress, or overdue/due-soon queries about system data.
- Always prefer a specific action intent (create/list/update/delete) over "general" when an action word is present.
- Use conversation history to resolve references like "those", "them", "the above", "from the summary", "from the file".

Also rate your confidence in this classification from 0.0 to 1.0:
- 1.0 = completely unambiguous
- 0.5 = plausible but the message could reasonably mean something else
- 0.0 = pure guess

Respond with ONLY a JSON object: {"intent": "<intent>", "confidence": <0.0-1.0>}"""

_CREATE_TASK_SYSTEM = """You are a task-creation assistant. Extract structured task data from the user's request.

IMPORTANT: If the user references content from earlier in the conversation (e.g. "those action items", "from the summary", "the tasks mentioned above", "create tasks from those"), you MUST look at the Conversation history provided and extract the actual specific tasks from there. Do NOT invent generic placeholder names like "Action Item" — always use the real, specific task names found in the prior conversation.

{users_block}
Return ONLY a JSON object with these fields (use null for unknown):
{{
  "tasks": [
    {{
      "name": "string (required — must be a specific, descriptive task title, never a generic placeholder)",
      "description": "string or null",
      "start_date": "YYYY-MM-DD or null",
      "due_date": "YYYY-MM-DD or null",
      "assignee_name": "string or null — use exact name from the known users list when possible",
      "project_name": "string or null",
      "team_name": "string or null",
      "status": "todo"
    }}
  ]
}}
Today's date: {today}"""

_UPDATE_TASK_SYSTEM = """You are a task-update assistant. Extract the update intent from the user's message.

IMPORTANT: If the user wants to update ALL tasks (e.g. "mark all tasks as done", "set everything to in_progress"), set task_reference to "__ALL__".
Otherwise set task_reference to the specific task ID number or title fragment.

Return ONLY a JSON object:
{
  "task_reference": "string — '__ALL__' for bulk update, or a task ID / title fragment for a specific task",
  "updates": {
    "name": "string or null",
    "status": "todo|in_progress|done|pending_review or null",
    "due_date": "YYYY-MM-DD or null",
    "assignee_name": "string or null"
  }
}"""

_ANALYZE_TEXT_SYSTEM = """You are a task extraction assistant. Read the provided text (and conversation history if given) and extract actionable tasks.

If the user asks to summarise or analyse content from earlier in the conversation, look at the Conversation history to find that content.

IMPORTANT: Only extract tasks from genuine source material — a pasted email, meeting transcript, document, or notes. If "User instruction" is just a short question, greeting, or command about the app itself (e.g. "explain my rocks", "what should I do today") with no actual document/transcript/email text to extract from, return an EMPTY tasks array. Never invent a task out of the user's own question — e.g. the message "explain my rocks" must NOT produce a task named anything like "Explain rocks" or "Explain User's Rocks".

{users_block}
Return ONLY a JSON object:
{{
  "tasks": [
    {{
      "name": "string (required, specific and descriptive task title — never a generic placeholder)",
      "description": "string or null",
      "start_date": "YYYY-MM-DD or null",
      "due_date": "YYYY-MM-DD or null",
      "assignee_name": "string or null — use exact name from the known users list when possible",
      "project_name": "string or null",
      "confidence": "low|medium|high"
    }}
  ],
  "summary": "string (1-2 sentence summary of what the text was about)"
}}
Today's date: {today}"""

_DB_QUERY_EXTRACT_SYSTEM = """You are a database query parameter extractor for a task management system.
Extract structured parameters from the user's question.

Return ONLY a JSON object:
{
  "sub_intent": "one of the values below",
  "user_name": "string or null — the name of a specific OTHER user mentioned (NOT the current user)",
  "project_name": "string or null — the name of a specific project mentioned",
  "team_name": "string or null — the name of a specific team mentioned",
  "status": "string or null — the status keyword mentioned, normalized to: todo|in_progress|pending_review|done|active|paused|completed|cancelled",
  "role": "admin|team_manager|team_member or null",
  "days_ahead": "integer or null — for due-soon queries, how many days ahead (default 7)",
  "target_self": "true or false — set true when the user refers to themselves with words like 'my', 'mine', 'I have', 'my list', 'my tasks'; set false when asking about another named user or everyone"
}

Sub-intent values and when to use them:
- user_count: "how many users", "total users", "number of users"
- user_list: "list all users", "show all users", "who are the users", "all users"
- user_by_role: "who are the admins", "show managers", "list team members", "who has role X"
- user_tasks: "tasks assigned to [name]", "what tasks does [name] have", "[name]'s tasks", "tasks for [name]"
- task_by_status: "how many tasks are done", "show all completed tasks", "list in_progress tasks", "tasks with status X"
- task_overdue: "overdue tasks", "what tasks are overdue", "past due tasks", "tasks that are late"
- task_by_project: "tasks in project X", "tasks for project Alpha", "what is project X working on"
- task_by_team: "tasks for team Y", "team Beta tasks", "what is team Y working on"
- task_due_soon: "tasks due soon", "tasks due this week", "upcoming tasks", "tasks due in X days"
- project_count: "how many projects", "total projects", "number of projects"
- project_list: "list all projects", "show projects", "what projects are there"
- project_by_status: "how many active projects", "completed projects", "projects with status X"
- project_progress: "progress of project X", "how is project Alpha going", "project X status", "how far along is project X"
- team_count: "how many teams", "total teams", "number of teams"
- team_list: "list all teams", "show teams", "what teams are there"
- team_members: "who is in team X", "members of team Beta", "team X members", "who belongs to team X"
- team_workload: "workload of team X", "team Alpha tasks", "how busy is team X", "team X task count"
- workload_summary: "overall summary", "workload overview", "system stats", "system overview", "dashboard stats", "give me a summary"
- rock_list: "show all rocks", "list rocks", "what rocks are there", "our quarterly rocks", "explain my rocks", "explain rocks"
- rock_by_team: "rocks for team X", "team Alpha's rocks", "what rocks does team X have"
- issue_list: "show all issues", "list issues", "what issues are there"
- issue_open: "open issues", "unresolved issues", "issues that aren't closed"
- kpi_list: "show our KPIs", "list KPIs", "what KPIs does team X have"
- kpi_progress: "how is KPI X doing", "progress on KPI X", "is KPI X on target"
- meeting_list: "show meetings", "list meetings", "what meetings are there"
- meeting_upcoming: "upcoming meetings", "next meeting", "what meetings are scheduled soon"
- client_request_list: "show client requests", "what requests have clients submitted", "pending task requests", "my submitted requests"

Status normalization:
- done/completed/finished/complete/closed → "done"
- todo/pending/not started/new/open → "todo"
- in progress/ongoing/working/started/wip → "in_progress"
- pending review/review/in review/reviewing/under review → "pending_review"
- active/running/ongoing → "active" (for projects)
- paused/on hold/suspended → "paused" (for projects)
- cancelled/canceled/abandoned → "cancelled" (for projects)

Role normalization:
- admin/administrator → "admin"
- manager/team manager → "team_manager"
- member/team member/employee → "team_member"
"""


def _today() -> str:
    return date.today().isoformat()


def _parse_json_safe(text: str) -> dict:
    cleaned = text.strip()
    for prefix in ("```json", "```"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3].strip()
    return json.loads(cleaned)


class ChatService:
    def __init__(self, db: AsyncSession, org_id):
        self._db = db
        self._org_id = org_id
        self._chat_repo = ChatRepository(db, org_id)
        self._task_repo = TaskRepository(db, org_id)
        self._user_repo = UserRepository(db)
        self._project_repo = ProjectRepository(db, org_id)
        self._team_repo = TeamRepository(db, org_id)
        self._llm = get_llm_provider()
        # Per-request caches — populated on first access, reused within the same message.
        self._users_cache: list | None = None
        self._projects_cache: list | None = None
        self._teams_cache: list | None = None

    # ─── Public entry point ───────────────────────────────────────────────────

    async def handle_message(
        self,
        *,
        user: User,
        message: str,
        session_id: int | None,
        org_role: str = TEAM_MEMBER,
        file_context: dict | None = None,
    ) -> ChatMessageResponse:
        """
        file_context (optional): {"filename": str, "text": str, "size_bytes": int}
        When provided the file content is injected into the LLM prompt and the
        stored user message is prefixed with a [📎 filename] badge.
        """
        # Observability (spec Section 49) — one trace_id per turn, threaded
        # through log lines and audit rows so every tool call made while
        # handling this single message can be correlated.
        # Stored on self rather than threaded through every method signature —
        # safe because a fresh ChatService instance is created per HTTP
        # request (see app/api/routes/chat.py), so this never leaks across turns.
        self._trace_id = str(uuid.uuid4())
        logger.info(
            "Chat turn started: trace_id=%s user=%s session=%s query=%r",
            self._trace_id, user.id, session_id, message,
        )

        # 1. Get or create session
        session = await self._get_or_create_session(user, session_id, message)

        # 2. Build stored message (with file badge when applicable)
        stored_user_message = _build_stored_message(message, file_context)
        user_msg = await self._chat_repo.add_message(session.id, "user", stored_user_message)

        # 3. Build conversation history for context
        history = await self._chat_repo.get_session_messages(session.id, limit=20)
        history_text = self._format_history(history[:-1])

        # 3b. Conversation state machine (spec Section 8) — if the user sends
        # a fresh message instead of confirming/cancelling a pending change
        # set via its dedicated action, treat that as an implicit
        # abandonment: nothing was ever applied, so silently letting the
        # stale preview lapse (rather than executing it later on a random
        # follow-up) is the safe behavior for "never execute ambiguous
        # writes". The change set itself still enforces staleness/expiry
        # independently if the user later does click its button.
        state_notice = ""
        if session.state == "awaiting_confirmation" and session.pending_change_set_id:
            pending_id = session.pending_change_set_id
            change_set = await change_sets.get_change_set(self._db, self._org_id, pending_id)
            if change_set is not None and change_set.status == "pending":
                await change_sets.cancel_change_set(self._db, change_set)
                state_notice = "_(I've dropped the pending confirmation since you moved on.)_\n\n"

        # 4. Build the effective prompt the LLM will see (file content injected)
        effective_message = _build_llm_prompt(message, file_context)

        # 5. When a file is attached with no explicit instruction, acknowledge it
        #    and ask the user what they want to do — never auto-execute anything.
        actions: list[ChatAction] = []
        if file_context and not message.strip():
            size_kb = round(file_context["size_bytes"] / 1024, 1)
            reply = (
                f"I've received **{file_context['filename']}** ({size_kb} KB). "
                "What would you like me to do with it?\n\n"
                "• **Summarise** the document\n"
                "• **Create tasks** from the content\n"
                "• **Answer questions** about it\n"
                "• **Extract key points or action items**\n\n"
                "Just tell me and I'll get started."
            )
            assistant_msg = await self._chat_repo.add_message(session.id, "assistant", reply)
            if len(history) <= 2 and session.title is None:
                await self._auto_title_session(session, file_context["filename"])
            await self._chat_repo.touch_session(session)
            return ChatMessageResponse(
                session_id=session.id,
                user_message=_msg_read(user_msg),
                assistant_message=_msg_read(assistant_msg),
                actions=[],
            )

        # 6. Structured Planner (spec Section 20, bounded) — a message with
        # multiple distinct goals ("create a task for X and tell me how many
        # tasks are overdue") is split into ordered, self-contained steps,
        # each run through the same single-intent pipeline below. Skipped
        # for file uploads, same as the query rewriter.
        steps = [message]
        if not file_context and message.strip():
            steps = await planner.maybe_split_goals(self._llm, message)
            if len(steps) > 1:
                logger.info("Planner split query into %d steps: %r", len(steps), steps)

        step_replies: list[str] = []
        actions: list[ChatAction] = []
        for step_message in steps:
            step_reply, step_actions = await self._detect_and_route_step(
                step_message, file_context, effective_message if len(steps) == 1 else None,
                history_text, org_role, session.id, user,
            )
            step_replies.append(step_reply)
            actions.extend(step_actions)

        reply = state_notice + (
            step_replies[0] if len(step_replies) == 1
            else "\n\n".join(f"**{i}.** {r}" for i, r in enumerate(step_replies, 1))
        )

        logger.info("Chat turn reply: trace_id=%s session=%s reply=%r", self._trace_id, session.id, reply)

        # 8. Persist assistant reply
        assistant_msg = await self._chat_repo.add_message(session.id, "assistant", reply)

        # 9. Auto-title session after first exchange (fire-and-forget — doesn't block response).
        if len(history) <= 2 and session.title is None:
            title_hint = file_context["filename"] if file_context else message
            asyncio.create_task(self._auto_title_session(session.id, title_hint))

        # 9b. Roll the session's topic summary forward (best-effort, same
        # fire-and-forget contract as auto-titling — never blocks the reply).
        if message.strip():
            asyncio.create_task(topics.update_topic(self._llm, session.id, message, reply))
            asyncio.create_task(
                memory.maybe_learn_preference(self._llm, org_id=self._org_id, user_id=user.id, message=message)
            )

        await self._chat_repo.touch_session(session)

        return ChatMessageResponse(
            session_id=session.id,
            user_message=_msg_read(user_msg),
            assistant_message=_msg_read(assistant_msg),
            actions=actions,
        )

    async def _detect_and_route_step(
        self, step_message: str, file_context: dict | None, precomputed_effective_message: str | None,
        history_text: str, org_role: str, session_id: int, user: User,
    ) -> tuple[str, list[ChatAction]]:
        """One iteration of query-rewrite -> intent-detect -> ambiguity-gate
        -> route, factored out so the Structured Planner can run it once per
        split-out goal instead of only once per whole message."""
        # Contextual Query Rewriter (spec Section 12) — expand a follow-up
        # like "assign it to her" into a self-contained instruction using
        # history. Skipped for file uploads (never rewrite around file content).
        effective_message = precomputed_effective_message
        if effective_message is None:
            rewritten = step_message
            if not file_context and step_message.strip():
                rewritten = await query_rewriter.rewrite_query(self._llm, step_message, history_text)
                if rewritten != step_message:
                    logger.info("Query rewritten: %r -> %r", step_message, rewritten)
            effective_message = _build_llm_prompt(rewritten, file_context)

        intent_input = step_message if file_context else effective_message
        intent, intent_confidence = await self._detect_intent(intent_input, history_text)
        logger.info(
            "Detected intent: %s (confidence=%.2f, file_attached=%s) for query=%r",
            intent, intent_confidence, bool(file_context), step_message,
        )

        if intent in self._AMBIGUITY_GATED_INTENTS and intent_confidence < self._AMBIGUITY_CONFIDENCE_FLOOR:
            return (
                "I'm not fully sure what you'd like me to do — could you rephrase that, "
                "e.g. \"create a task to...\", \"update task 12 to...\", or \"delete task 12\"?",
                [],
            )
        try:
            return await self._route(intent, user, effective_message, history_text, org_role, session_id)
        except Exception as exc:
            logger.exception("Error handling intent %s: %s", intent, exc)
            return "I ran into an issue processing that. Could you try rephrasing?", []

    # ─── Session management ───────────────────────────────────────────────────

    async def _get_or_create_session(
        self, user: User, session_id: int | None, first_message: str
    ) -> "ChatSession":
        if session_id:
            session = await self._chat_repo.get_session(session_id)
            if session and session.user_id == user.id:
                return session
        return await self._chat_repo.create_session(user.id)

    async def _auto_title_session(self, session_id: int, message: str) -> None:
        # Fire-and-forget from handle_message via asyncio.create_task — must
        # never touch self._db, which belongs to the request's own coroutine
        # and may already be committing/closing by the time this runs. Opens
        # its own session, same pattern as background_email.py.
        try:
            result = await self._llm.generate_text(
                system_prompt=(
                    "Generate a short (max 6 words) title summarising this chat message. "
                    "Return ONLY the title text, nothing else."
                ),
                user_prompt=message[:300],
                temperature=0.3,
            )
            title = result.text.strip().strip('"').strip("'")[:255]
            if not title:
                return
            async with AsyncSessionLocal() as db:
                chat_repo = ChatRepository(db, self._org_id)
                session = await chat_repo.get_session(session_id)
                if session is not None:
                    await chat_repo.update_session_title(session, title)
        except Exception:
            pass  # non-critical

    # ─── Intent detection ─────────────────────────────────────────────────────

    async def _detect_intent(self, message: str, history: str) -> tuple[str, float]:
        prompt = f"Previous conversation:\n{history}\n\nUser message: {message}" if history else message
        try:
            result = await self._llm.generate_text(
                system_prompt=_INTENT_SYSTEM,
                user_prompt=prompt,
                temperature=0.0,
                response_format="json",
            )
            data = _parse_json_safe(result.text)
            try:
                confidence = float(data.get("confidence", 1.0))
            except (TypeError, ValueError):
                confidence = 1.0
            return data.get("intent", INTENT_GENERAL), confidence
        except Exception as exc:
            logger.warning("Intent detection failed: %s", exc)
            return INTENT_GENERAL, 0.0

    # Ambiguity Engine (spec Section 15, bounded) — below this confidence, a
    # mutation-risk intent is not routed automatically; the user is asked to
    # confirm what they meant instead of the assistant silently guessing.
    _AMBIGUITY_CONFIDENCE_FLOOR = 0.55
    _AMBIGUITY_GATED_INTENTS = {INTENT_CREATE_TASK, INTENT_UPDATE_TASK, INTENT_DELETE_TASK, INTENT_CONVERT_REQUEST}

    # ─── Router ───────────────────────────────────────────────────────────────

    _TASK_MUTATION_INTENTS = {INTENT_CREATE_TASK, INTENT_UPDATE_TASK, INTENT_DELETE_TASK, INTENT_CONVERT_REQUEST}

    async def _route(
        self, intent: str, user: User, message: str, history: str, org_role: str = TEAM_MEMBER,
        session_id: int | None = None,
    ) -> tuple[str, list[ChatAction]]:
        if org_role == TEAM_MEMBER and intent in self._TASK_MUTATION_INTENTS:
            return (
                "You don't have permission to create, update, or delete tasks through the assistant. "
                "Please contact your team manager or admin to make task changes.",
                [],
            )
        from app.core.org_roles import CLIENT as _CLIENT_ROLE
        if org_role == _CLIENT_ROLE and intent == INTENT_CONVERT_REQUEST:
            return "Converting task requests is handled by your project manager.", []
        if intent == INTENT_CREATE_TASK:
            return await self._handle_create_task(user, message, history)
        if intent == INTENT_LIST_TASKS:
            return await self._handle_list_tasks(user, message, history, org_role)
        if intent == INTENT_UPDATE_TASK:
            return await self._handle_update_task(user, message, history, session_id, org_role)
        if intent == INTENT_DELETE_TASK:
            return await self._handle_delete_task(user, message, history, session_id, org_role)
        if intent == INTENT_CONVERT_REQUEST:
            return await self._handle_convert_request(user, message, history, session_id, org_role)
        if intent == INTENT_ANALYZE_TEXT:
            return await self._handle_analyze_text(user, message, history)
        if intent == INTENT_DB_QUERY:
            return await self._handle_db_query(user, message, history, org_role)
        return await self._handle_general(user, message, history, session_id)

    # ─── Create task ──────────────────────────────────────────────────────────

    async def _handle_create_task(
        self, user: User, message: str, history: str = ""
    ) -> tuple[str, list[ChatAction]]:
        users_block = await self._build_users_block()
        system = (
            _CREATE_TASK_SYSTEM
            .replace("{users_block}", users_block)
            .replace("{today}", _today())
        )
        user_prompt = (
            f"Conversation history:\n{history}\n\nUser instruction: {message}"
            if history else message
        )
        result = await self._llm.generate_text(
            system_prompt=system,
            user_prompt=user_prompt,
            temperature=0.1,
            response_format="json",
        )
        data = _parse_json_safe(result.text)
        raw_tasks: list[dict] = data.get("tasks", [])

        if not raw_tasks:
            return "I couldn't extract any tasks from that. Could you be more specific about what needs to be done?", []

        created = []
        actions: list[ChatAction] = []

        for raw in raw_tasks:
            name = (raw.get("name") or "").strip()
            if not name:
                continue

            assignee_id = await self._resolve_user_id(raw.get("assignee_name"), fallback=user.id)
            project_id = await self._resolve_project_id(raw.get("project_name"))
            team_id = await self._resolve_team_id(raw.get("team_name"))

            payload = TaskCreate(
                name=name,
                start_date=_parse_date(raw.get("start_date")),
                due_date=_parse_date(raw.get("due_date")),
                assignee_id=assignee_id,
                project_id=project_id,
                team_id=team_id,
                status=raw.get("status", "todo"),
            )
            task = await self._task_repo.create(payload, created_by_id=user.id)
            await self._notify_assigned(task, assigned_by=user)
            created.append(task)
            actions.append(
                ChatAction(
                    type="task_created",
                    label=f'Task created: "{task.name}"',
                    payload={"task_id": task.id, "task_name": task.name},
                )
            )

        if not created:
            return "I understood you want to create tasks but couldn't parse the details. Could you provide more specific task names?", []

        names = ", ".join(f'"{t.name}"' for t in created)
        reply = (
            f"Done! I created {len(created)} task{'s' if len(created) > 1 else ''}: {names}. "
            "You can view and manage them on the Tasks page."
        )
        return reply, actions

    # ─── List tasks ───────────────────────────────────────────────────────────

    async def _handle_list_tasks(
        self, user: User, message: str, history: str = "", org_role: str = TEAM_MEMBER
    ) -> tuple[str, list[ChatAction]]:
        from app.core.org_roles import ADMIN, OWNER, TEAM_MANAGER

        msg_lower = message.lower()
        self_ref = any(w in msg_lower for w in ["my task", "my list", "my todo", "i have", "assigned to me", "my work"])

        # Org-scoped role is authoritative (see app/core/org_roles.py) — a
        # user's global User.role column is only a display/default value and
        # commonly stale (e.g. an org owner whose account originally
        # registered as a plain member elsewhere).
        if org_role in {OWNER, ADMIN, TEAM_MANAGER} and not self_ref:
            tasks = await self._task_repo.list_all()
        else:
            tasks = await self._task_repo.list_for_assignee(user.id)

        if not tasks:
            return "You have no tasks at the moment.", []

        # Ask LLM to summarise / filter based on the user's query
        task_list_text = "\n".join(
            f"- [{t.id}] {t.name} | status={t.status} | due={t.due_date or 'no date'} "
            f"| assignee={t.assignee.full_name if t.assignee else 'unassigned'}"
            for t in tasks[:50]
        )

        history_block = f"Conversation history:\n{history}\n\n" if history else ""
        result = await self._llm.generate_text(
            system_prompt=(
                "You are a task management assistant. The user has asked about their tasks. "
                "Given the list of tasks below, answer the user's question in a helpful, concise way. "
                "Use bullet points. Reference task IDs in brackets like [42]. "
                "If the user asked for a summary, give a brief overview grouped by status."
            ),
            user_prompt=f"{history_block}User question: {message}\n\nTasks:\n{task_list_text}",
            temperature=0.2,
        )

        actions: list[ChatAction] = [
            ChatAction(type="navigate", label="View all tasks", payload={"path": "/tasks"})
        ]
        return result.text, actions

    # ─── Update task ──────────────────────────────────────────────────────────

    async def _handle_update_task(
        self, user: User, message: str, history: str = "", session_id: int | None = None,
        org_role: str = TEAM_MEMBER,
    ) -> tuple[str, list[ChatAction]]:
        from app.core.org_roles import ADMIN, OWNER, TEAM_MANAGER
        _can_see_all = org_role in {OWNER, ADMIN, TEAM_MANAGER}

        user_prompt = (
            f"Conversation history:\n{history}\n\nUser instruction: {message}"
            if history else message
        )
        result = await self._llm.generate_text(
            system_prompt=_UPDATE_TASK_SYSTEM,
            user_prompt=user_prompt,
            temperature=0.1,
            response_format="json",
        )
        data = _parse_json_safe(result.text)

        ref = (data.get("task_reference") or "").strip()
        updates_raw: dict = data.get("updates", {})

        # Build the update payload from LLM output
        update_payload: dict = {}
        if updates_raw.get("name"):
            update_payload["name"] = updates_raw["name"]
        if updates_raw.get("status"):
            update_payload["status"] = updates_raw["status"]
        if updates_raw.get("due_date"):
            update_payload["due_date"] = _parse_date(updates_raw["due_date"])
        if updates_raw.get("assignee_name"):
            users = await self._get_users()
            resolution = reference_resolver.resolve_by_name(
                users, updates_raw["assignee_name"], lambda u: u.full_name, lambda u: u.id,
            )
            if resolution.status == ResolutionStatus.AMBIGUOUS:
                return resolution.clarification_message_for("person"), []
            if resolution.entity is not None:
                update_payload["assignee_id"] = resolution.entity.id

        if not update_payload:
            return "I understood you want to update a task but couldn't determine what to change. Could you be more specific?", []

        # ── Bulk update — always previewed and confirmed (risk R4), never
        #    applied immediately, regardless of which fields are touched. ──
        if ref == "__ALL__":
            if _can_see_all:
                all_tasks = await self._task_repo.list_all()
            else:
                all_tasks = await self._task_repo.list_for_assignee(user.id)

            if not all_tasks:
                return "There are no tasks to update.", []

            changes = ", ".join(f"{k}={v}" for k, v in update_payload.items())
            change_set = await change_sets.build_change_set(
                self._db,
                org_id=self._org_id, session_id=session_id, user_id=user.id,
                tool_name="update_task_bulk",
                params={"task_ids": [t.id for t in all_tasks], "updates": update_payload},
                affected_tasks=all_tasks,
                affected_summary=f"{len(all_tasks)} task(s): {changes}",
            )

            # Multi-Level Approvals (spec Section 47) — a team_manager's
            # bulk update needs an admin's sign-off, not just their own
            # confirmation; see risk.requires_admin_approval.
            if risk.requires_admin_approval("update_task_bulk", org_role):
                await approvals.create_approval_request(
                    self._db, org_id=self._org_id, session_id=session_id, change_set=change_set,
                    requested_by_id=user.id, approver_role="admin",
                    reason=f"Bulk update of {len(all_tasks)} task(s): {changes}",
                )
                await self._db.commit()
                return (
                    f"This bulk update affects **{len(all_tasks)} task(s)**: {changes}. "
                    "Since this is a large change, I've sent it to an admin for approval before it's applied.",
                    [],
                )

            await self._db.commit()
            return (
                f"This will update **{len(all_tasks)} task(s)**: {changes}. Please confirm to proceed.",
                [ChatAction(
                    type="change_set_preview",
                    label=f"Confirm bulk update of {len(all_tasks)} task(s)",
                    payload={"change_set_id": change_set.id, "affected_count": len(all_tasks), "summary": changes},
                )],
            )

        # ── Single task update ────────────────────────────────────────────────
        task = None
        if ref.isdigit():
            task = await self._task_repo.get_by_id(int(ref))
        else:
            if _can_see_all:
                all_tasks = await self._task_repo.list_all()
            else:
                all_tasks = await self._task_repo.list_for_assignee(user.id)

            resolution = await reference_resolver.resolve_task_reference(self._db, all_tasks, ref)
            if resolution.status == ResolutionStatus.AMBIGUOUS:
                return resolution.clarification_message, []
            task = resolution.entity

        if not task:
            return (
                f'I couldn\'t find a task matching "{ref}". '
                "Please check the Tasks page or provide the task ID.",
                [],
            )

        # Reassignment (changing who a task belongs to) is risk R3 — preview
        # and confirm rather than apply immediately. Simple field edits
        # (status/name/due_date) stay R2 — today's instant-apply behavior.
        if update_payload.get("assignee_id") and update_payload["assignee_id"] != task.assignee_id:
            changes = ", ".join(f"{k}={v}" for k, v in update_payload.items())
            change_set = await change_sets.build_change_set(
                self._db,
                org_id=self._org_id, session_id=session_id, user_id=user.id,
                tool_name="reassign_task",
                params={"task_id": task.id, "updates": update_payload},
                affected_tasks=[task],
                affected_summary=f'"{task.name}": {changes}',
            )
            await self._db.commit()
            return (
                f'This will update "{task.name}": {changes}. Please confirm to proceed.',
                [ChatAction(
                    type="change_set_preview",
                    label=f'Confirm reassignment of "{task.name}"',
                    payload={"change_set_id": change_set.id, "affected_count": 1, "summary": changes},
                )],
            )

        updated = await self._task_repo.update(task, TaskUpdate(**update_payload))

        changes = ", ".join(f"{k}={v}" for k, v in update_payload.items())
        return f'Updated task "{updated.name}": {changes}.', [
            ChatAction(
                type="task_updated",
                label=f'Task updated: "{updated.name}"',
                payload={"task_id": updated.id},
            )
        ]

    # ─── Delete task ──────────────────────────────────────────────────────────

    async def _handle_delete_task(
        self, user: User, message: str, history: str = "", session_id: int | None = None,
        org_role: str = TEAM_MEMBER,
    ) -> tuple[str, list[ChatAction]]:
        from app.core.org_roles import ADMIN, OWNER, TEAM_MANAGER

        # Org-scoped role is authoritative — see the note in
        # _handle_list_tasks. Pre-existing gate also widened to include
        # OWNER, which was previously excluded outright.
        if org_role not in {OWNER, ADMIN, TEAM_MANAGER}:
            return "Only admins and team managers can delete tasks. Please ask your manager.", []

        user_prompt = (
            f"Conversation history:\n{history}\n\nUser instruction: {message}"
            if history else message
        )
        result = await self._llm.generate_text(
            system_prompt=(
                "Extract what the user wants to delete.\n"
                "- If they want to delete ALL tasks (e.g. 'delete all tasks', 'remove everything', 'delete all of task'), "
                "set task_reference to '__ALL__'.\n"
                "- Otherwise set task_reference to the specific task ID number or title fragment.\n"
                "Use conversation history to resolve vague references like 'that task' or 'those'.\n"
                'Return ONLY JSON: {"task_reference": "string"}'
            ),
            user_prompt=user_prompt,
            temperature=0.0,
            response_format="json",
        )
        data = _parse_json_safe(result.text)
        ref = (data.get("task_reference") or "").strip()

        # ── Bulk delete — blocked outright (risk R7), never executed from
        #    chat regardless of confirmation. A chat confirmation is too thin
        #    a safeguard for wiping every task in the organization at once. ──
        if ref == "__ALL__":
            return (
                "Mass-deleting every task isn't available through the assistant, for safety. "
                "Please delete tasks individually from the Tasks page, or ask an admin.",
                [],
            )

        # ── Single task delete ────────────────────────────────────────────────
        task = None
        if ref.isdigit():
            task = await self._task_repo.get_by_id(int(ref))
        else:
            if org_role in {OWNER, ADMIN, TEAM_MANAGER}:
                all_tasks = await self._task_repo.list_all()
            else:
                all_tasks = await self._task_repo.list_for_assignee(user.id)
            resolution = await reference_resolver.resolve_task_reference(self._db, all_tasks, ref)
            if resolution.status == ResolutionStatus.AMBIGUOUS:
                return resolution.clarification_message, []
            task = resolution.entity

        if not task:
            return (
                f'I couldn\'t find a task matching "{ref}". '
                "Please check the Tasks page or provide the exact task ID.",
                [],
            )

        # Single delete is risk R3 — irreversible, so preview and confirm
        # rather than delete immediately.
        change_set = await change_sets.build_change_set(
            self._db,
            org_id=self._org_id, session_id=session_id, user_id=user.id,
            tool_name="delete_task_single",
            params={"task_id": task.id},
            affected_tasks=[task],
            affected_summary=f'"{task.name}"',
        )
        await self._db.commit()
        return (
            f'This will permanently delete "{task.name}". This cannot be undone. Please confirm.',
            [ChatAction(
                type="change_set_preview",
                label=f'Confirm deletion of "{task.name}"',
                payload={"change_set_id": change_set.id, "affected_count": 1, "summary": f'delete "{task.name}"'},
            )],
        )

    # ─── Convert client task request ───────────────────────────────────────────

    async def _handle_convert_request(
        self, user: User, message: str, history: str = "", session_id: int | None = None,
        org_role: str = TEAM_MEMBER,
    ) -> tuple[str, list[ChatAction]]:
        from app.core.org_roles import ADMIN, OWNER, TEAM_MANAGER
        if org_role not in {OWNER, ADMIN, TEAM_MANAGER}:
            return "Only admins and team managers can convert client requests into tasks.", []

        user_prompt = (
            f"Conversation history:\n{history}\n\nUser instruction: {message}"
            if history else message
        )
        result = await self._llm.generate_text(
            system_prompt=(
                "Extract which client task request the user wants converted into a real task.\n"
                "Set request_reference to the request ID number if given, otherwise a fragment of its title.\n"
                'Return ONLY JSON: {"request_reference": "string"}'
            ),
            user_prompt=user_prompt,
            temperature=0.0,
            response_format="json",
        )
        data = _parse_json_safe(result.text)
        ref = (data.get("request_reference") or "").strip()
        if not ref:
            return "Which request would you like to convert? Please give me its ID or part of its title.", []

        pending = await self._query_client_requests(user=user, org_role=org_role)
        pending = [r for r in pending if r.status == "pending"]
        if not pending:
            return "There are no pending client requests to convert.", []

        request = None
        if ref.isdigit():
            request = next((r for r in pending if r.id == int(ref)), None)
        else:
            resolution = reference_resolver.resolve_by_name(pending, ref, lambda r: r.title, lambda r: r.id)
            if resolution.status == ResolutionStatus.AMBIGUOUS:
                return resolution.clarification_message_for("request"), []
            request = resolution.entity

        if request is None:
            return f'I couldn\'t find a pending request matching "{ref}".', []

        change_set = await change_sets.build_change_set(
            self._db,
            org_id=self._org_id, session_id=session_id, user_id=user.id,
            tool_name="convert_client_request_to_task",
            params={"request_id": request.id},
            affected_tasks=[],
            affected_summary=f'convert request "{request.title}" into a task',
        )
        await self._db.commit()
        return (
            f'This will convert the client request "{request.title}" into a real task. Please confirm.',
            [ChatAction(
                type="change_set_preview",
                label=f'Confirm conversion of "{request.title}"',
                payload={"change_set_id": change_set.id, "affected_count": 1, "summary": f'convert "{request.title}"'},
            )],
        )

    # ─── Analyze text ─────────────────────────────────────────────────────────

    # ─── Per-request caches ───────────────────────────────────────────────────

    async def _get_users(self) -> list:
        if self._users_cache is None:
            self._users_cache = await self._user_repo.list_all()
        return self._users_cache

    async def _get_projects(self) -> list:
        if self._projects_cache is None:
            self._projects_cache = await self._project_repo.list_all()
        return self._projects_cache

    async def _get_teams(self) -> list:
        if self._teams_cache is None:
            self._teams_cache = await self._team_repo.list_all()
        return self._teams_cache

    async def _build_users_block(self) -> str:
        all_users = await self._get_users()
        if not all_users:
            return ""
        lines = "\n".join(f"- {u.full_name} <{u.email}>" for u in all_users)
        return f"Known system users (match assignee names to this list):\n{lines}\n\n"

    async def _handle_analyze_text(
        self, user: User, message: str, history: str = ""
    ) -> tuple[str, list[ChatAction]]:
        users_block = await self._build_users_block()
        system = (
            _ANALYZE_TEXT_SYSTEM
            .replace("{users_block}", users_block)
            .replace("{today}", _today())
        )
        user_prompt = (
            f"Conversation history:\n{history}\n\nUser instruction: {message}"
            if history else message
        )
        result = await self._llm.generate_text(
            system_prompt=system,
            user_prompt=user_prompt,
            temperature=0.1,
            response_format="json",
        )
        data = _parse_json_safe(result.text)

        # Defense-in-depth against the extraction prompt fabricating a task
        # out of a short question/command (e.g. "explain my rocks") rather
        # than genuine pasted content — never write a low-confidence guess
        # to the database, even if the prompt above still tries to.
        raw_tasks: list[dict] = [
            t for t in data.get("tasks", []) if (t.get("confidence") or "").lower() != "low"
        ]
        summary: str = data.get("summary", "")

        if not raw_tasks:
            summary_line = f"\n\nSummary: {summary}" if summary else ""
            return f"I analyzed the text but couldn't find any clear action items.{summary_line}", []

        # Create tasks in the DB
        created = []
        actions: list[ChatAction] = []
        for raw in raw_tasks:
            name = (raw.get("name") or "").strip()
            if not name:
                continue
            assignee_id = await self._resolve_user_id(raw.get("assignee_name"), fallback=user.id)
            project_id = await self._resolve_project_id(raw.get("project_name"))
            team_id = await self._resolve_team_id(raw.get("team_name"))

            payload = TaskCreate(
                name=name,
                start_date=_parse_date(raw.get("start_date")),
                due_date=_parse_date(raw.get("due_date")),
                assignee_id=assignee_id,
                project_id=project_id,
                team_id=team_id,
            )
            task = await self._task_repo.create(payload, created_by_id=user.id)
            created.append(task)
            actions.append(
                ChatAction(
                    type="task_created",
                    label=f'Task created: "{task.name}"',
                    payload={"task_id": task.id, "task_name": task.name},
                )
            )

        summary_line = f"\n\n**Summary:** {summary}" if summary else ""
        reply = (
            f"I extracted **{len(created)} task{'s' if len(created) > 1 else ''}** from the text:{summary_line}\n\n"
            + "\n".join(f"• {t.name}" for t in created)
        )
        return reply, actions

    # ─── DB Query ─────────────────────────────────────────────────────────────

    async def _handle_db_query(
        self, user: User, message: str, history: str = "", org_role: str = TEAM_MEMBER,
    ) -> tuple[str, list[ChatAction]]:
        """Thin observability wrapper (spec Section 49) — logs one audit row
        per db_query turn (read tools were previously unaudited; only writes
        went through audit.log_tool_execution) before delegating to the
        actual sub-intent dispatch below."""
        reply, actions = await self._handle_db_query_impl(user, message, history, org_role)
        try:
            await audit.log_tool_execution(
                self._db, org_id=self._org_id, session_id=None, user_id=user.id,
                tool_name="db_query", risk_level="R0", policy_decision="allow",
                params={"message": message[:500]}, result_summary=reply[:500], success=True,
                trace_id=getattr(self, "_trace_id", None),
            )
        except Exception:
            logger.info("db_query audit logging skipped (non-critical)", exc_info=True)
        return reply, actions

    async def _handle_db_query_impl(
        self, user: User, message: str, history: str = "", org_role: str = TEAM_MEMBER
    ) -> tuple[str, list[ChatAction]]:
        from app.core.org_roles import ADMIN, CLIENT, OWNER, TEAM_MANAGER

        # Security: refuse any request that attempts data modification
        _destructive = {"insert", "drop", "truncate", "alter"}
        msg_words = set(message.lower().split())
        if msg_words & _destructive:
            return (
                "I can only read data from the database. "
                "I cannot modify, delete, or alter any records. "
                "To make changes, please use the application interface.",
                [],
            )

        # Extract sub-intent and entities via LLM
        user_prompt = (
            f"Conversation history:\n{history}\n\nUser question: {message}"
            if history else message
        )
        try:
            result = await self._llm.generate_text(
                system_prompt=_DB_QUERY_EXTRACT_SYSTEM,
                user_prompt=user_prompt,
                temperature=0.0,
                response_format="json",
            )
            params = _parse_json_safe(result.text)
        except Exception as exc:
            logger.warning("DB query extraction failed: %s — falling back to general", exc)
            return await self._handle_general(user, message, history)

        sub_intent: str = params.get("sub_intent") or ""
        user_name: str | None = params.get("user_name")
        project_name: str | None = params.get("project_name")
        team_name: str | None = params.get("team_name")
        raw_status: str | None = params.get("status")
        role: str | None = params.get("role")
        days_ahead: int = int(params.get("days_ahead") or 7)
        target_self: bool = str(params.get("target_self", "false")).lower() == "true"

        status = _normalize_status(raw_status)
        project_status = _normalize_project_status(raw_status)

        if org_role == CLIENT and policy.is_client_blocked_sub_intent(sub_intent):
            return policy.CLIENT_REFUSAL_MESSAGE, []

        logger.info(
            "DB query routed: sub_intent=%s user=%r project=%r team=%r status=%r role=%r",
            sub_intent, user_name, project_name, team_name, status, role,
        )

        can_see_all = org_role in {OWNER, ADMIN, TEAM_MANAGER}

        # ── user_count ────────────────────────────────────────────────────────
        if sub_intent == "user_count":
            if not can_see_all:
                return "You don't have permission to view user statistics.", []
            users = await self._user_repo.list_all()
            active = sum(1 for u in users if u.is_active)
            return (
                f"There are **{len(users)} user(s)** in the system "
                f"({active} active, {len(users) - active} inactive)."
            ), []

        # ── user_list ─────────────────────────────────────────────────────────
        if sub_intent == "user_list":
            if not can_see_all:
                return "You don't have permission to list all users.", []
            users = await self._user_repo.list_all()
            if not users:
                return "There are no users in the system.", []
            lines = [
                f"• **{u.full_name}** ({u.email}) — {u.role.replace('_', ' ')} "
                f"— {'active' if u.is_active else 'inactive'}"
                for u in users[:50]
            ]
            return f"**Users ({len(users)} total):**\n" + "\n".join(lines), []

        # ── user_by_role ──────────────────────────────────────────────────────
        if sub_intent == "user_by_role":
            if not can_see_all:
                return "You don't have permission to view user roles.", []
            if not role:
                return "Which role are you asking about? (admin, team_manager, or team_member)", []
            users = await self._user_repo.list_by_roles([role])
            if not users:
                return f"There are no users with role **{role.replace('_', ' ')}**.", []
            names = ", ".join(u.full_name for u in users[:30])
            label = role.replace("_", " ").title() + "s"
            return f"**{label} ({len(users)}):** {names}", []

        # ── user_tasks ────────────────────────────────────────────────────────
        if sub_intent == "user_tasks":
            if user_name:
                target = await self._resolve_user_by_name(user_name)
                if not target:
                    return f"I couldn't find a user named **{user_name}**. Please check the name.", []
                if not can_see_all and target.id != user.id:
                    return "You can only view your own tasks.", []
                target_id = target.id
                target_label = target.full_name
            elif target_self or not can_see_all:
                # User is asking about their own tasks ("my tasks", "my list", etc.)
                target_id = user.id
                target_label = "You"
            else:
                return "Which user are you asking about?", []

            tasks = await self._task_repo.list_for_assignee(target_id)
            if not tasks:
                return f"**{target_label}** has no assigned tasks.", []

            if status:
                filtered = [t for t in tasks if t.status == status]
                if not filtered:
                    return f"**{target_label}** has no tasks with status **{status}**.", []
                lines = [
                    f"• [{t.id}] {t.name} — due {t.due_date or 'no date'}"
                    for t in filtered[:25]
                ]
                return (
                    f"**{target_label}** has **{len(filtered)} {status.replace('_', ' ')} task(s)**:\n\n"
                    + "\n".join(lines)
                ), []

            counts = {s: sum(1 for t in tasks if t.status == s) for s in ["todo", "in_progress", "pending_review", "done"]}
            lines = [
                f"• [{t.id}] {t.name} — {t.status} — due {t.due_date or 'no date'}"
                for t in tasks[:25]
            ]
            summary = (
                f"**{target_label}** has **{len(tasks)} task(s)**: "
                f"todo={counts['todo']}, in_progress={counts['in_progress']}, "
                f"pending_review={counts['pending_review']}, done={counts['done']}"
            )
            return f"{summary}\n\n" + "\n".join(lines), []

        # ── task_by_status ────────────────────────────────────────────────────
        if sub_intent == "task_by_status":
            tasks = (
                await self._task_repo.list_for_assignee(user.id)
                if target_self
                else await self._task_repo.list_all()
                if can_see_all
                else await self._task_repo.list_for_assignee(user.id)
            )
            if status:
                filtered = [t for t in tasks if t.status == status]
                if not filtered:
                    return f"There are no tasks with status **{status}**.", []
                lines = [
                    f"• [{t.id}] {t.name} "
                    f"— {t.assignee.full_name if t.assignee else 'unassigned'} "
                    f"— due {t.due_date or 'no date'}"
                    for t in filtered[:30]
                ]
                return f"**Tasks with status '{status}' ({len(filtered)}):**\n" + "\n".join(lines), []
            # No specific status — show breakdown
            counts = {s: sum(1 for t in tasks if t.status == s) for s in ["todo", "in_progress", "pending_review", "done"]}
            return (
                f"**Task status breakdown ({len(tasks)} total):**\n"
                f"• To Do: {counts['todo']}\n"
                f"• In Progress: {counts['in_progress']}\n"
                f"• Pending Review: {counts['pending_review']}\n"
                f"• Done: {counts['done']}"
            ), []

        # ── task_overdue ──────────────────────────────────────────────────────
        if sub_intent == "task_overdue":
            today = date.today()
            tasks = (
                await self._task_repo.list_for_assignee(user.id)
                if target_self
                else await self._task_repo.list_all()
                if can_see_all
                else await self._task_repo.list_for_assignee(user.id)
            )
            overdue = [
                t for t in tasks
                if t.due_date and t.due_date < today and t.status != "done"
            ]
            if not overdue:
                return "There are no overdue tasks.", []
            lines = [
                f"• [{t.id}] {t.name} — due {t.due_date} — {t.status} "
                f"— {t.assignee.full_name if t.assignee else 'unassigned'}"
                for t in overdue[:30]
            ]
            return f"**Overdue tasks ({len(overdue)}):**\n" + "\n".join(lines), []

        # ── task_by_project ───────────────────────────────────────────────────
        if sub_intent == "task_by_project":
            if not project_name:
                return "Which project are you asking about?", []
            project = await self._resolve_project(project_name)
            if not project or (org_role == CLIENT and not await self._project_repo.is_member(project.id, user.id)):
                return f"I couldn't find a project named **{project_name}**. Check the Projects page.", []
            tasks = await self._task_repo.list_by_project(project.id)
            if not tasks:
                return f"Project **{project.name}** has no tasks.", []
            counts = {s: sum(1 for t in tasks if t.status == s) for s in ["todo", "in_progress", "pending_review", "done"]}
            lines = [
                f"• [{t.id}] {t.name} — {t.status} "
                f"— {t.assignee.full_name if t.assignee else 'unassigned'}"
                for t in tasks[:30]
            ]
            summary = (
                f"**Project '{project.name}' — {len(tasks)} task(s):** "
                f"todo={counts['todo']}, in_progress={counts['in_progress']}, "
                f"pending_review={counts['pending_review']}, done={counts['done']}"
            )
            return f"{summary}\n\n" + "\n".join(lines), []

        # ── task_by_team ──────────────────────────────────────────────────────
        if sub_intent == "task_by_team":
            if not team_name:
                return "Which team are you asking about?", []
            team = await self._resolve_team(team_name)
            if not team:
                return f"I couldn't find a team named **{team_name}**. Check the Teams page.", []
            tasks = await self._task_repo.list_by_team(team.id)
            if not tasks:
                return f"Team **{team.name}** has no tasks.", []
            lines = [
                f"• [{t.id}] {t.name} — {t.status} "
                f"— {t.assignee.full_name if t.assignee else 'unassigned'}"
                for t in tasks[:30]
            ]
            return f"**Team '{team.name}' tasks ({len(tasks)}):**\n" + "\n".join(lines), []

        # ── task_due_soon ─────────────────────────────────────────────────────
        if sub_intent == "task_due_soon":
            today = date.today()
            cutoff = today + timedelta(days=days_ahead)
            tasks = (
                await self._task_repo.list_for_assignee(user.id)
                if target_self
                else await self._task_repo.list_all()
                if can_see_all
                else await self._task_repo.list_for_assignee(user.id)
            )
            due_soon = [
                t for t in tasks
                if t.due_date and today <= t.due_date <= cutoff and t.status != "done"
            ]
            if not due_soon:
                return f"No tasks are due in the next {days_ahead} day(s).", []
            lines = [
                f"• [{t.id}] {t.name} — due {t.due_date} — {t.status} "
                f"— {t.assignee.full_name if t.assignee else 'unassigned'}"
                for t in due_soon[:30]
            ]
            return f"**Tasks due in the next {days_ahead} day(s) ({len(due_soon)}):**\n" + "\n".join(lines), []

        # ── project_count ─────────────────────────────────────────────────────
        if sub_intent == "project_count":
            if not can_see_all:
                return "You don't have permission to view project statistics.", []
            projects = await self._project_repo.list_all()
            counts = {s: sum(1 for p in projects if p.status == s) for s in ["active", "paused", "completed", "cancelled"]}
            return (
                f"There are **{len(projects)} project(s)** total: "
                f"active={counts['active']}, paused={counts['paused']}, "
                f"completed={counts['completed']}, cancelled={counts['cancelled']}."
            ), []

        # ── project_list ──────────────────────────────────────────────────────
        if sub_intent == "project_list":
            if not can_see_all:
                return "You don't have permission to list all projects.", []
            projects = await self._project_repo.list_all()
            if not projects:
                return "There are no projects in the system.", []
            lines = [f"• [{p.id}] **{p.name}** — {p.status}" for p in projects[:30]]
            return f"**Projects ({len(projects)} total):**\n" + "\n".join(lines), []

        # ── project_by_status ─────────────────────────────────────────────────
        if sub_intent == "project_by_status":
            if not can_see_all:
                return "You don't have permission to view project statistics.", []
            projects = await self._project_repo.list_all()
            if project_status:
                filtered = [p for p in projects if p.status == project_status]
                if not filtered:
                    return f"There are no projects with status **{project_status}**.", []
                lines = [f"• [{p.id}] **{p.name}**" for p in filtered[:30]]
                return f"**{project_status.title()} projects ({len(filtered)}):**\n" + "\n".join(lines), []
            counts = {s: sum(1 for p in projects if p.status == s) for s in ["active", "paused", "completed", "cancelled"]}
            return (
                f"**Project status breakdown ({len(projects)} total):**\n"
                f"• Active: {counts['active']}\n"
                f"• Paused: {counts['paused']}\n"
                f"• Completed: {counts['completed']}\n"
                f"• Cancelled: {counts['cancelled']}"
            ), []

        # ── project_progress ──────────────────────────────────────────────────
        if sub_intent == "project_progress":
            if not project_name:
                return "Which project are you asking about?", []
            project = await self._resolve_project(project_name)
            if not project or (org_role == CLIENT and not await self._project_repo.is_member(project.id, user.id)):
                return f"I couldn't find a project named **{project_name}**.", []
            tasks = await self._task_repo.list_by_project(project.id)
            total = len(tasks)
            if total == 0:
                return f"Project **{project.name}** has no tasks yet. Status: {project.status}.", []
            counts = {s: sum(1 for t in tasks if t.status == s) for s in ["todo", "in_progress", "pending_review", "done"]}
            pct = round(counts["done"] / total * 100)
            return (
                f"**Project '{project.name}' — {project.status}**\n"
                f"Progress: {counts['done']}/{total} tasks done ({pct}%)\n"
                f"• To Do: {counts['todo']}\n"
                f"• In Progress: {counts['in_progress']}\n"
                f"• Pending Review: {counts['pending_review']}\n"
                f"• Done: {counts['done']}"
            ), []

        # ── team_count ────────────────────────────────────────────────────────
        if sub_intent == "team_count":
            if not can_see_all:
                return "You don't have permission to view team statistics.", []
            teams = await self._team_repo.list_all()
            return f"There are **{len(teams)} team(s)** in the system.", []

        # ── team_list ─────────────────────────────────────────────────────────
        if sub_intent == "team_list":
            if not can_see_all:
                return "You don't have permission to list all teams.", []
            teams = await self._team_repo.list_all()
            if not teams:
                return "There are no teams in the system.", []
            lines = [
                f"• **{t.name}** — managed by "
                f"{t.team_manager.full_name if t.team_manager else 'unassigned'} "
                f"— {len(t.memberships)} member(s)"
                for t in teams[:30]
            ]
            return f"**Teams ({len(teams)} total):**\n" + "\n".join(lines), []

        # ── team_members ──────────────────────────────────────────────────────
        if sub_intent == "team_members":
            if not team_name:
                return "Which team are you asking about?", []
            team = await self._resolve_team(team_name)
            if not team:
                return f"I couldn't find a team named **{team_name}**.", []
            members = [m.user for m in team.memberships if m.user]
            if not members:
                return f"Team **{team.name}** has no members.", []
            lines = [f"• {m.full_name} ({m.role.replace('_', ' ')})" for m in members]
            return f"**{team.name} — {len(members)} member(s):**\n" + "\n".join(lines), []

        # ── team_workload ─────────────────────────────────────────────────────
        if sub_intent == "team_workload":
            if not team_name:
                return "Which team are you asking about?", []
            team = await self._resolve_team(team_name)
            if not team:
                return f"I couldn't find a team named **{team_name}**.", []
            tasks = await self._task_repo.list_by_team(team.id)
            if not tasks:
                return f"Team **{team.name}** has no tasks.", []
            counts = {s: sum(1 for t in tasks if t.status == s) for s in ["todo", "in_progress", "pending_review", "done"]}
            per_member: dict[str, int] = {}
            for t in tasks:
                label = t.assignee.full_name if t.assignee else "Unassigned"
                per_member[label] = per_member.get(label, 0) + 1
            member_lines = [
                f"• {name}: {count} task(s)"
                for name, count in sorted(per_member.items(), key=lambda x: -x[1])
            ]
            return (
                f"**Team '{team.name}' workload — {len(tasks)} task(s):**\n"
                f"todo={counts['todo']}, in_progress={counts['in_progress']}, "
                f"pending_review={counts['pending_review']}, done={counts['done']}\n\n"
                + "\n".join(member_lines)
            ), []

        # ── workload_summary ──────────────────────────────────────────────────
        if sub_intent == "workload_summary":
            if not can_see_all:
                tasks = await self._task_repo.list_for_assignee(user.id)
                counts = {s: sum(1 for t in tasks if t.status == s) for s in ["todo", "in_progress", "pending_review", "done"]}
                return (
                    f"**Your workload ({len(tasks)} task(s)):**\n"
                    f"• To Do: {counts['todo']}\n"
                    f"• In Progress: {counts['in_progress']}\n"
                    f"• Pending Review: {counts['pending_review']}\n"
                    f"• Done: {counts['done']}"
                ), []
            tasks = await self._task_repo.list_all()
            users = await self._user_repo.list_all()
            projects = await self._project_repo.list_all()
            teams = await self._team_repo.list_all()
            today = date.today()
            overdue = sum(1 for t in tasks if t.due_date and t.due_date < today and t.status != "done")
            counts = {s: sum(1 for t in tasks if t.status == s) for s in ["todo", "in_progress", "pending_review", "done"]}
            return (
                f"**System overview:**\n"
                f"• Users: {len(users)}\n"
                f"• Teams: {len(teams)}\n"
                f"• Projects: {len(projects)}\n"
                f"• Tasks: {len(tasks)} total\n"
                f"  — To Do: {counts['todo']}\n"
                f"  — In Progress: {counts['in_progress']}\n"
                f"  — Pending Review: {counts['pending_review']}\n"
                f"  — Done: {counts['done']}\n"
                f"  — Overdue: {overdue}"
            ), []

        # ── rock_list ─────────────────────────────────────────────────────────
        if sub_intent == "rock_list":
            rocks = await self._query_rocks()
            if not rocks:
                return "There are no Rocks in the system.", []
            team_names = await self._team_name_lookup()
            lines = [
                f"• **{r.title}** — {r.status.replace('_', ' ')} "
                f"— team: {team_names.get(r.team_id, 'unknown')}"
                f"{' — due ' + r.due_date.isoformat() if r.due_date else ''}"
                for r in rocks[:30]
            ]
            return f"**Rocks ({len(rocks)} total):**\n" + "\n".join(lines), []

        # ── rock_by_team ──────────────────────────────────────────────────────
        if sub_intent == "rock_by_team":
            if not team_name:
                return "Which team are you asking about?", []
            team = await self._resolve_team(team_name)
            if not team:
                return f"I couldn't find a team named **{team_name}**.", []
            rocks = await self._query_rocks(team_id=team.id)
            if not rocks:
                return f"Team **{team.name}** has no Rocks.", []
            lines = [
                f"• **{r.title}** — {r.status.replace('_', ' ')}"
                f"{' — due ' + r.due_date.isoformat() if r.due_date else ''}"
                for r in rocks[:30]
            ]
            return f"**{team.name} — Rocks ({len(rocks)}):**\n" + "\n".join(lines), []

        # ── issue_list ────────────────────────────────────────────────────────
        if sub_intent == "issue_list":
            issues = await self._query_issues()
            if not issues:
                return "There are no Issues in the system.", []
            team_names = await self._team_name_lookup()
            lines = [
                f"• **{i.title}** — {i.status.replace('_', ' ')} "
                f"— team: {team_names.get(i.team_id, 'unknown')}"
                for i in issues[:30]
            ]
            return f"**Issues ({len(issues)} total):**\n" + "\n".join(lines), []

        # ── issue_open ────────────────────────────────────────────────────────
        if sub_intent == "issue_open":
            issues = await self._query_issues(open_only=True)
            if not issues:
                return "There are no open Issues. 🎉", []
            team_names = await self._team_name_lookup()
            lines = [
                f"• **{i.title}** — team: {team_names.get(i.team_id, 'unknown')}"
                f"{' — assigned to ' + i.assignee.full_name if i.assignee else ''}"
                for i in issues[:30]
            ]
            return f"**Open issues ({len(issues)}):**\n" + "\n".join(lines), []

        # ── kpi_list / kpi_progress ──────────────────────────────────────────
        if sub_intent in ("kpi_list", "kpi_progress"):
            team_id = await self._resolve_team_id(team_name) if team_name else None
            kpis = await self._query_kpis(team_id)
            if not kpis:
                return "There are no KPIs to show.", []
            latest_by_kpi = await self._latest_kpi_entries([k.id for k in kpis])
            lines = []
            for k in kpis[:30]:
                entry = latest_by_kpi.get(k.id)
                value_text = f"{entry.value}" if entry and entry.value is not None else "no data yet"
                target_text = f" (target {k.reference_value})" if k.reference_value is not None else ""
                lines.append(f"• **{k.title}**: {value_text}{target_text}")
            return f"**KPIs ({len(kpis)}):**\n" + "\n".join(lines), []

        # ── meeting_list / meeting_upcoming ──────────────────────────────────
        if sub_intent in ("meeting_list", "meeting_upcoming"):
            upcoming_only = sub_intent == "meeting_upcoming"
            meetings = await self._query_meetings(upcoming_only=upcoming_only)
            if not meetings:
                return "There are no meetings to show." if not upcoming_only else "There are no upcoming meetings.", []
            team_names = await self._team_name_lookup()
            lines = [
                f"• **{m.title}** — {m.scheduled_at.strftime('%Y-%m-%d %H:%M')} "
                f"— team: {team_names.get(m.team_id, 'unknown')} — {m.status.replace('_', ' ')}"
                for m in meetings[:30]
            ]
            label = "Upcoming meetings" if upcoming_only else "Meetings"
            return f"**{label} ({len(meetings)}):**\n" + "\n".join(lines), []

        # ── client_request_list ──────────────────────────────────────────────
        if sub_intent == "client_request_list":
            requests = await self._query_client_requests(user=user, org_role=org_role)
            if not requests:
                return "There are no task requests to show.", []
            lines = [
                f"• **{r.title}** — {r.status.replace('_', ' ')} "
                f"— project: {r.project.name if r.project else 'unknown'}"
                for r in requests[:30]
            ]
            return f"**Client task requests ({len(requests)}):**\n" + "\n".join(lines), []

        # Unknown sub-intent — fall back to general
        logger.info("DB query sub_intent %r unrecognized — falling back to general", sub_intent)
        return await self._handle_general(user, message, history)

    # ─── Rocks / Issues (org-scoped, direct queries — no dedicated repository) ──

    async def _query_rocks(self, team_id: int | None = None) -> list[Rock]:
        stmt = select(Rock).where(
            Rock.organization_id == self._org_id, Rock.is_archived.is_(False)
        ).order_by(Rock.created_at.desc())
        if team_id is not None:
            stmt = stmt.where(Rock.team_id == team_id)
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def _query_issues(self, *, open_only: bool = False) -> list[Issue]:
        # Issue.assignee is already lazy="selectin" on the model — no explicit
        # eager-load needed here.
        stmt = select(Issue).where(Issue.organization_id == self._org_id).order_by(Issue.created_at.desc())
        if open_only:
            stmt = stmt.where(Issue.status != "resolved")
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def _team_name_lookup(self) -> dict[int, str]:
        teams = await self._get_teams()
        return {t.id: t.name for t in teams}

    async def _query_kpis(self, team_id: int | None = None) -> list["KPI"]:
        stmt = select(KPI).where(KPI.organization_id == self._org_id, KPI.is_snoozed.is_(False)).order_by(KPI.sort_order)
        if team_id is not None:
            stmt = stmt.where(KPI.team_id == team_id)
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def _latest_kpi_entries(self, kpi_ids: list[int]) -> dict[int, "KPIEntry"]:
        if not kpi_ids:
            return {}
        stmt = (
            select(KPIEntry)
            .where(KPIEntry.kpi_id.in_(kpi_ids))
            .order_by(KPIEntry.kpi_id, KPIEntry.period_start.desc())
        )
        result = await self._db.execute(stmt)
        latest: dict[int, KPIEntry] = {}
        for entry in result.scalars().all():
            if entry.kpi_id not in latest:
                latest[entry.kpi_id] = entry
        return latest

    async def _query_meetings(self, *, upcoming_only: bool = False) -> list["Meeting"]:
        stmt = select(Meeting).where(Meeting.organization_id == self._org_id).order_by(Meeting.scheduled_at)
        if upcoming_only:
            stmt = stmt.where(Meeting.scheduled_at >= datetime.now(timezone.utc), Meeting.status == "scheduled")
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def _query_client_requests(self, *, user: User, org_role: str) -> list["TaskRequest"]:
        from app.core.org_roles import ADMIN, CLIENT, OWNER, TEAM_MANAGER

        stmt = (
            select(TaskRequest)
            .where(TaskRequest.organization_id == self._org_id)
            .order_by(TaskRequest.created_at.desc())
        )
        if org_role == CLIENT:
            # A client only ever sees their own submitted requests.
            stmt = stmt.where(TaskRequest.submitted_by_id == user.id)
        elif org_role not in {OWNER, ADMIN, TEAM_MANAGER}:
            stmt = stmt.where(TaskRequest.submitted_by_id == user.id)
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    # ─── General Q&A ─────────────────────────────────────────────────────────

    async def _handle_general(
        self, user: User, message: str, history: str, session_id: int | None = None,
    ) -> tuple[str, list[ChatAction]]:
        # Context Packer (spec Sections 18-19) — topic summary + saved
        # preferences, explicitly labeled as advisory/overridable by any
        # live tool result, per [[conflict resolver]] convention.
        topic_summary = ""
        if session_id is not None:
            active_topic = await topics.get_active_topic(self._db, session_id)
            if active_topic is not None:
                topic_summary = active_topic.summary
        saved_memories = await memory.get_saved_memories(self._db, user.id)
        packed_context = context_packer.pack_context(
            topic_summary=topic_summary, saved_memories=saved_memories, history=history,
        )

        result = await self._llm.generate_text(
            system_prompt=(
                f"You are a helpful AI assistant for {user.full_name} in a task management application. "
                "Answer the user's question using your knowledge. "
                "You can help with: task management strategies, productivity tips, project management advice, "
                "workflow optimization, prioritization techniques, and general questions. "
                "If the user is asking about specific data in the system (e.g. their task counts, "
                "user lists, project status), let them know they can ask more specifically and you'll look it up. "
                "Keep responses concise — under 200 words unless more detail is explicitly requested."
            ),
            user_prompt=(
                f"{packed_context}\n\nUser: {message}" if packed_context else message
            ),
            temperature=0.4,
        )
        return result.text, []

    # ─── Notification + email helpers ────────────────────────────────────────

    async def _notify_assigned(self, task, assigned_by: User) -> None:
        """Create an in-app notification and enqueue an assignment email."""
        # Capture plain integer IDs before any commits to avoid ORM identity-map
        # issues — the Task model has multiple FK columns pointing to User
        # (assignee_id, created_by_id, completed_by_id, reviewed_by_id) and
        # SQLAlchemy can resolve get_by_id() to the wrong cached object.
        task_id: int = task.id
        task_name: str = task.name
        assignee_id: int | None = task.assignee_id
        assigned_by_id: int = assigned_by.id

        if not assignee_id or assignee_id == assigned_by_id:
            return

        self._db.add(
            Notification(
                user_id=assignee_id,
                task_id=task_id,
                title="New task assigned to you",
                message=f"You have been assigned a new task: '{task_name}'.",
                type="task_assigned",
            )
        )
        await self._db.commit()

        # Use a fresh UserRepository call with the captured plain integer ID so
        # the session identity map cannot return a stale / wrong User object.
        assignee = await self._user_repo.get_by_id(assignee_id)
        if assignee:
            logger.info(
                "Scheduling task_assigned email | task_id=%s | assignee_id=%s"
                " | assignee_email=%s | assigned_by_id=%s",
                task_id, assignee.id, assignee.email, assigned_by_id,
            )
            asyncio.create_task(bg_send_task_assigned(task_id, assignee.id, assigned_by_id))
        else:
            logger.warning(
                "task_assigned email skipped — assignee not found in DB"
                " | task_id=%s | assignee_id=%s",
                task_id, assignee_id,
            )

    # ─── Helpers ──────────────────────────────────────────────────────────────

    async def _resolve_user_id(
        self, name: str | None, fallback: int | None = None
    ) -> int | None:
        if not name:
            return fallback
        users = await self._get_users()
        name_lower = name.lower()
        for u in users:
            if name_lower in u.full_name.lower() or name_lower in u.email.lower():
                return u.id
        return fallback

    async def _resolve_user_by_name(self, name: str) -> User | None:
        users = await self._get_users()
        name_lower = name.lower()
        for u in users:
            if name_lower in u.full_name.lower() or name_lower in u.email.lower():
                return u
        return None

    async def _resolve_project_id(self, name: str | None) -> int | None:
        if not name:
            return None
        projects = await self._get_projects()
        name_lower = name.lower()
        for p in projects:
            if name_lower in p.name.lower():
                return p.id
        return None

    async def _resolve_project(self, name: str):
        projects = await self._get_projects()
        name_lower = name.lower()
        for p in projects:
            if name_lower in p.name.lower():
                return p
        return None

    async def _resolve_team_id(self, name: str | None) -> int | None:
        if not name:
            return None
        teams = await self._get_teams()
        name_lower = name.lower()
        for t in teams:
            if name_lower in t.name.lower():
                return t.id
        return None

    async def _resolve_team(self, name: str):
        teams = await self._get_teams()
        name_lower = name.lower()
        for t in teams:
            if name_lower in t.name.lower():
                return t
        return None

    @staticmethod
    def _format_history(messages: list) -> str:
        lines = []
        for m in messages[-10:]:
            role = "User" if m.role == "user" else "Assistant"
            # Allow longer assistant messages so summaries/analyses are fully visible
            limit = 2000 if m.role == "assistant" else 400
            content = m.content[:limit]
            if len(m.content) > limit:
                content += " [...]"
            lines.append(f"{role}: {content}")
        return "\n".join(lines)


# ─── File context helpers ─────────────────────────────────────────────────────

def _build_stored_message(message: str, file_context: dict | None) -> str:
    """What gets saved to the database as the user's message."""
    if not file_context:
        return message
    badge = f"[📎 {file_context['filename']}]"
    return f"{badge}\n\n{message}".strip() if message.strip() else badge


def _build_llm_prompt(message: str, file_context: dict | None) -> str:
    """What gets sent to the LLM (includes raw file text)."""
    if not file_context:
        return message
    file_block = (
        f"[ATTACHED FILE: {file_context['filename']}]\n"
        f"{file_context['text']}\n"
        f"[END OF FILE]"
    )
    user_part = message.strip() or "Please analyse this file and extract any actionable tasks."
    return f"{file_block}\n\n[USER MESSAGE]\n{user_part}"


# ─── Serialisation helper ─────────────────────────────────────────────────────

def _msg_read(msg: "ChatMessage"):
    from app.schemas.chat import ChatMessageRead

    return ChatMessageRead(
        id=msg.id,
        session_id=msg.session_id,
        role=msg.role,
        content=msg.content,
        created_at=msg.created_at,
    )


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
