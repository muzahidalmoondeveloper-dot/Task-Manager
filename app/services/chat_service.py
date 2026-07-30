"""Chat service: intent detection + natural-language task management."""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

import asyncio

from app.models.notification import Notification
from app.models.user import User
from app.repositories.chat_repository import ChatRepository
from app.repositories.task_repository import TaskRepository
from app.repositories.user_repository import UserRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.team_repository import TeamRepository
from app.core.org_roles import TEAM_MEMBER
from app.schemas.chat import ChatAction, ChatMessageResponse
from app.schemas.task import TaskCreate, TaskUpdate
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
- analyze_text: user pasted an email, meeting transcript, or document and wants tasks extracted OR wants a summary/analysis of previously shared content
- db_query: user is asking an analytical or lookup question about data in the system — e.g. "how many users are there", "list all projects", "what tasks are overdue", "who is in team Alpha", "what is the progress of project X", "how many tasks are done", "show active projects", "what tasks does John have", "system statistics", "workload summary", "who are the admins", "how many teams", "tasks due this week"
- general: any other question, greeting, or request not covered above

IMPORTANT rules:
- "delete all", "delete all tasks", "delete all of task", "remove all" → delete_task
- "mark all as done", "set all to complete", "update all tasks" → update_task
- Always prefer db_query over general when the user asks about counts, lists, statistics, user names, roles, team members, project progress, or overdue/due-soon queries about system data.
- Always prefer a specific action intent (create/list/update/delete) over "general" when an action word is present.
- Use conversation history to resolve references like "those", "them", "the above", "from the summary", "from the file".

Respond with ONLY a JSON object: {"intent": "<intent>"}"""

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
        # 1. Get or create session
        session = await self._get_or_create_session(user, session_id, message)

        # 2. Build stored message (with file badge when applicable)
        stored_user_message = _build_stored_message(message, file_context)
        user_msg = await self._chat_repo.add_message(session.id, "user", stored_user_message)

        # 3. Build conversation history for context
        history = await self._chat_repo.get_session_messages(session.id, limit=20)
        history_text = self._format_history(history[:-1])

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

        # 6. Detect intent from the user's typed message only — never from file
        #    content — so the file never silently triggers actions on its own.
        intent_input = message if file_context else effective_message
        intent = await self._detect_intent(intent_input, history_text)
        logger.info("Detected intent: %s (file_attached=%s)", intent, bool(file_context))

        # 7. Route to handler (execution uses effective_message so LLM has file text)
        try:
            reply, actions = await self._route(intent, user, effective_message, history_text, org_role)
        except Exception as exc:
            logger.exception("Error handling intent %s: %s", intent, exc)
            reply = "I ran into an issue processing that. Could you try rephrasing?"

        # 8. Persist assistant reply
        assistant_msg = await self._chat_repo.add_message(session.id, "assistant", reply)

        # 9. Auto-title session after first exchange (fire-and-forget — doesn't block response).
        if len(history) <= 2 and session.title is None:
            title_hint = file_context["filename"] if file_context else message
            asyncio.create_task(self._auto_title_session(session, title_hint))

        await self._chat_repo.touch_session(session)

        return ChatMessageResponse(
            session_id=session.id,
            user_message=_msg_read(user_msg),
            assistant_message=_msg_read(assistant_msg),
            actions=actions,
        )

    # ─── Session management ───────────────────────────────────────────────────

    async def _get_or_create_session(
        self, user: User, session_id: int | None, first_message: str
    ) -> "ChatSession":
        if session_id:
            session = await self._chat_repo.get_session(session_id)
            if session and session.user_id == user.id:
                return session
        return await self._chat_repo.create_session(user.id)

    async def _auto_title_session(self, session: "ChatSession", message: str) -> None:
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
            if title:
                await self._chat_repo.update_session_title(session, title)
        except Exception:
            pass  # non-critical

    # ─── Intent detection ─────────────────────────────────────────────────────

    async def _detect_intent(self, message: str, history: str) -> str:
        prompt = f"Previous conversation:\n{history}\n\nUser message: {message}" if history else message
        try:
            result = await self._llm.generate_text(
                system_prompt=_INTENT_SYSTEM,
                user_prompt=prompt,
                temperature=0.0,
                response_format="json",
            )
            data = _parse_json_safe(result.text)
            return data.get("intent", INTENT_GENERAL)
        except Exception as exc:
            logger.warning("Intent detection failed: %s", exc)
            return INTENT_GENERAL

    # ─── Router ───────────────────────────────────────────────────────────────

    _TASK_MUTATION_INTENTS = {INTENT_CREATE_TASK, INTENT_UPDATE_TASK, INTENT_DELETE_TASK}

    async def _route(
        self, intent: str, user: User, message: str, history: str, org_role: str = TEAM_MEMBER
    ) -> tuple[str, list[ChatAction]]:
        if org_role == TEAM_MEMBER and intent in self._TASK_MUTATION_INTENTS:
            return (
                "You don't have permission to create, update, or delete tasks through the assistant. "
                "Please contact your team manager or admin to make task changes.",
                [],
            )
        if intent == INTENT_CREATE_TASK:
            return await self._handle_create_task(user, message, history)
        if intent == INTENT_LIST_TASKS:
            return await self._handle_list_tasks(user, message, history)
        if intent == INTENT_UPDATE_TASK:
            return await self._handle_update_task(user, message, history)
        if intent == INTENT_DELETE_TASK:
            return await self._handle_delete_task(user, message, history)
        if intent == INTENT_ANALYZE_TEXT:
            return await self._handle_analyze_text(user, message, history)
        if intent == INTENT_DB_QUERY:
            return await self._handle_db_query(user, message, history)
        return await self._handle_general(user, message, history)

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
        self, user: User, message: str, history: str = ""
    ) -> tuple[str, list[ChatAction]]:
        from app.core.org_roles import ADMIN, TEAM_MANAGER

        msg_lower = message.lower()
        self_ref = any(w in msg_lower for w in ["my task", "my list", "my todo", "i have", "assigned to me", "my work"])

        if user.role in {ADMIN, TEAM_MANAGER} and not self_ref:
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
        self, user: User, message: str, history: str = ""
    ) -> tuple[str, list[ChatAction]]:
        from app.core.org_roles import ADMIN, TEAM_MANAGER

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
            uid = await self._resolve_user_id(updates_raw["assignee_name"])
            if uid:
                update_payload["assignee_id"] = uid

        if not update_payload:
            return "I understood you want to update a task but couldn't determine what to change. Could you be more specific?", []

        # ── Bulk update ───────────────────────────────────────────────────────
        if ref == "__ALL__":
            if user.role in {ADMIN, TEAM_MANAGER}:
                all_tasks = await self._task_repo.list_all()
            else:
                all_tasks = await self._task_repo.list_for_assignee(user.id)

            if not all_tasks:
                return "There are no tasks to update.", []

            count = 0
            actions: list[ChatAction] = []
            for t in all_tasks:
                await self._task_repo.update(t, TaskUpdate(**update_payload))
                count += 1
                actions.append(
                    ChatAction(type="task_updated", label=f'Updated: "{t.name}"', payload={"task_id": t.id})
                )

            changes = ", ".join(f"{k}={v}" for k, v in update_payload.items())
            return (
                f"Done. Updated **{count} task{'s' if count != 1 else ''}**: {changes}.",
                actions,
            )

        # ── Single task update ────────────────────────────────────────────────
        task = None
        if ref.isdigit():
            task = await self._task_repo.get_by_id(int(ref))
        else:
            if user.role in {ADMIN, TEAM_MANAGER}:
                all_tasks = await self._task_repo.list_all()
            else:
                all_tasks = await self._task_repo.list_for_assignee(user.id)

            ref_lower = ref.lower()
            matches = [t for t in all_tasks if ref_lower in t.name.lower()]
            if matches:
                task = matches[0]

        if not task:
            return (
                f'I couldn\'t find a task matching "{ref}". '
                "Please check the Tasks page or provide the task ID.",
                [],
            )

        old_assignee_id = task.assignee_id
        updated = await self._task_repo.update(task, TaskUpdate(**update_payload))

        if update_payload.get("assignee_id") and update_payload["assignee_id"] != old_assignee_id:
            await self._notify_assigned(updated, assigned_by=user)

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
        self, user: User, message: str, history: str = ""
    ) -> tuple[str, list[ChatAction]]:
        from app.core.org_roles import ADMIN, TEAM_MANAGER

        if user.role not in {ADMIN, TEAM_MANAGER}:
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

        # ── Bulk delete ───────────────────────────────────────────────────────
        if ref == "__ALL__":
            all_tasks = await self._task_repo.list_all()
            if not all_tasks:
                return "There are no tasks to delete.", []
            count = len(all_tasks)
            for t in all_tasks:
                await self._db.delete(t)
            await self._db.commit()
            return (
                f"Done. All **{count} task{'s' if count != 1 else ''}** have been deleted.",
                [ChatAction(type="task_deleted", label=f"Deleted all {count} tasks", payload={"count": count})],
            )

        # ── Single task delete ────────────────────────────────────────────────
        task = None
        if ref.isdigit():
            task = await self._task_repo.get_by_id(int(ref))
        else:
            if user.role in {ADMIN, TEAM_MANAGER}:
                all_tasks = await self._task_repo.list_all()
            else:
                all_tasks = await self._task_repo.list_for_assignee(user.id)
            ref_lower = ref.lower()
            matches = [t for t in all_tasks if ref_lower in t.name.lower()]
            if matches:
                task = matches[0]

        if not task:
            return (
                f'I couldn\'t find a task matching "{ref}". '
                "Please check the Tasks page or provide the exact task ID.",
                [],
            )

        name = task.name
        await self._task_repo.delete(task)
        return f'Task "{name}" has been deleted.', [
            ChatAction(type="task_deleted", label=f'Deleted: "{name}"', payload={"task_name": name})
        ]

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

        raw_tasks: list[dict] = data.get("tasks", [])
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
        self, user: User, message: str, history: str = ""
    ) -> tuple[str, list[ChatAction]]:
        from app.core.org_roles import ADMIN, TEAM_MANAGER

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

        logger.info(
            "DB query routed: sub_intent=%s user=%r project=%r team=%r status=%r role=%r",
            sub_intent, user_name, project_name, team_name, status, role,
        )

        can_see_all = user.role in {ADMIN, TEAM_MANAGER}

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
            if not project:
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
            if not project:
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

        # Unknown sub-intent — fall back to general
        logger.info("DB query sub_intent %r unrecognized — falling back to general", sub_intent)
        return await self._handle_general(user, message, history)

    # ─── General Q&A ─────────────────────────────────────────────────────────

    async def _handle_general(
        self, user: User, message: str, history: str
    ) -> tuple[str, list[ChatAction]]:
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
                f"Conversation so far:\n{history}\n\nUser: {message}" if history else message
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
