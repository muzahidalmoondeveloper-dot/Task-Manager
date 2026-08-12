"""Chat service: intent detection + natural-language task management."""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

import asyncio

from app.core.database import AsyncSessionLocal
from app.core.log_context import set_chat_context
from app.models.issue import Issue
from app.models.kpi import KPI, KPIEntry
from app.models.meeting import Meeting
from app.models.chat import ChatSession
from app.models.notification import Notification
from app.models.organization import Organization
from app.models.rock import Rock
from app.models.task_request import TaskRequest
from app.models.user import User
from app.repositories.chat_repository import ChatRepository
from app.repositories.task_repository import TaskRepository
from app.repositories.user_repository import UserRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.team_repository import TeamRepository
from app.core.org_roles import TEAM_MEMBER
from app.schemas.chat import ChatAction, ChatMessageResponse, PageContext
from app.services.copilot import (
    approvals, audit, change_sets, context_packer, memory, planner, policy, query_rewriter,
    reference_resolver, risk, topics,
)
from app.services.copilot.intent_schemas import (
    DbQueryExtraction,
    IntentDetectionResult,
    CreateKnowledgeDocumentExtraction,
    CreateProjectExtraction,
    CreateTeamExtraction,
    DeleteTaskExtraction,
    GenerateReportExtraction,
    ManageIssueExtraction,
    ManageMeetingExtraction,
    ManageProjectExtraction,
    ManageRockExtraction,
    ManageTeamExtraction,
    RecordKpiExtraction,
    SubmitClientRequestExtraction,
    UpdateTaskExtraction,
)
from app.services.copilot.reference_resolver import ResolutionStatus
from app.services.copilot.tools import ToolContext, check_write_authorized, run_tool
from app.services.copilot.tools import task_tools  # noqa: F401 — registers task_* tools on import
from app.services.llm import get_llm_provider
from app.services.background_email import bg_send_task_assigned

if TYPE_CHECKING:
    from app.models.chat import ChatMessage
    from app.models.task import Task

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
# Domain buildout (strict acceptance audit) — natural-language entry points
# for the new Issue/Rock/KPI/Client-Request write tools, mirroring the
# existing task intents' shape (one intent covers both create and update,
# same as how a single "update_task" already covers every field change).
INTENT_MANAGE_ISSUE = "manage_issue"
INTENT_MANAGE_ROCK = "manage_rock"
INTENT_RECORD_KPI = "record_kpi"
INTENT_SUBMIT_CLIENT_REQUEST = "submit_client_request"
INTENT_MANAGE_MEETING = "manage_meeting"
INTENT_CREATE_PROJECT = "create_project"
INTENT_CREATE_TEAM = "create_team"
INTENT_MANAGE_KNOWLEDGE = "manage_knowledge"  # architecture item 1 — save a document/SOP into the knowledge base
INTENT_MANAGE_PROJECT = "manage_project"  # architecture item 2 — update/archive an existing project
INTENT_MANAGE_TEAM = "manage_team"  # architecture item 2 — update a team, or reassign its manager
INTENT_GENERATE_REPORT = "generate_report"  # architecture item 3 — generate a project status report

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
- list_tasks: user wants to SEE/SHOW/FIND/LIST their tasks (e.g. "show my tasks", "list all tasks", "find my tasks") — NOT a counting/quantity question (those are db_query; see the rule below)
- update_task: user wants to change or update tasks — including bulk operations like "mark all tasks as done", "set everything to in_progress"
- delete_task: user wants to delete or remove tasks — including bulk operations like "delete all tasks", "remove all tasks", "delete all of task", "please delete all of task"
- analyze_text: user pasted an email, meeting transcript, or document IN THIS MESSAGE and wants tasks extracted, OR explicitly asks to summarise/analyse a document/transcript/email they shared earlier in this conversation (e.g. "summarise that email", "extract tasks from the transcript above"). Do NOT use this for a short question or command that merely mentions an app entity (tasks, Rocks, KPIs, projects, issues) — those are db_query or general, even if the wording contains "explain" or "summarise" (e.g. "explain my rocks", "summarise my week" are db_query, not analyze_text — there is no pasted document to extract from).
- db_query: user is asking an analytical or lookup question about data in the system — e.g. "how many users are there", "list all projects", "what tasks are overdue", "who is in team Alpha", "what is the progress of project X", "how many tasks are done", "show active projects", "what tasks does John have", "system statistics", "workload summary", "who are the admins", "how many teams", "tasks due this week", "list all issues", "show open issues", "what rocks does team X have", "show all rocks", "show our KPIs", "how is KPI X doing", "upcoming meetings", "list meetings", "client requests", "task requests from clients", "explain my rocks", "explain my tasks", "summarise my week"
- convert_request_to_task: user wants to turn a submitted client task request into a real task — e.g. "convert that request into a task", "approve the client's request and make it a task", "turn request 5 into a task"
- manage_issue: user wants to create a new issue, or change an existing issue's status/resolution — e.g. "log an issue about the server outage", "create an issue for team Alpha", "mark issue 5 as resolved", "resolve the login issue"
- manage_rock: user wants to create a new Rock (quarterly goal), or change an existing Rock's status — e.g. "create a rock for team Alpha to launch the new site", "mark rock 3 as on track", "set the onboarding rock to at risk"
- record_kpi: user wants to record/update/log a value for a KPI — e.g. "record 42 for the signups KPI", "log this week's revenue as 10000", "update the KPI value to 55"
- submit_client_request: a CLIENT user wants to submit a new task request — e.g. "I'd like to request a new feature", "can you add a task for X", "submit a request for the website project" (staff asking to create a task use create_task instead, never this)
- manage_meeting: user wants to schedule a new meeting, or reschedule/cancel/start/end an existing one — e.g. "schedule a meeting with team Alpha tomorrow at 3pm", "move the standup to 4pm", "cancel the retro meeting", "start the standup meeting", "end the retro meeting", "the client call is over"
- create_project: user wants to create a brand-new project — e.g. "create a project called Website Redesign", "start a new project for the Q2 launch"
- create_team: user wants to create a brand-new team — e.g. "create a team called Marketing managed by Sarah", "set up a new engineering team"
- manage_knowledge: user explicitly wants to SAVE/ADD/DOCUMENT something new into the knowledge base/SOPs (not just ask about existing docs — that's db_query/search_knowledge) — e.g. "save this as an SOP", "add this to the knowledge base", "document our refund policy: ...", "create a knowledge base article about onboarding"
- manage_project: user wants to update, rename, or archive an EXISTING project — e.g. "rename project Alpha to Beta", "mark the website project as completed", "archive the old CRM project", "pause project X" (creating a brand-new project is create_project, not this)
- manage_team: user wants to update an EXISTING team's name/description, or change who manages a team — e.g. "rename team Alpha to Growth", "change the description of team Beta", "make Sarah the manager of team Alpha", "reassign team Beta to John" (creating a brand-new team is create_team, not this)
- generate_report: user explicitly wants to GENERATE/CREATE a status report for a project — e.g. "generate a monthly report for project Alpha", "create a client report for the website project", "give me a weekly status report for project X"
- general: any other question, greeting, or request not covered above

IMPORTANT rules:
- "delete all", "delete all tasks", "delete all of task", "remove all" → delete_task
- "mark all as done", "set all to complete", "update all tasks" → update_task
- Always prefer db_query over general when the user asks about counts, lists, statistics, user names, roles, team members, project progress, or overdue/due-soon queries about system data.
- Always prefer a specific action intent (create/list/update/delete) over "general" when an action word is present.
- Use conversation history to resolve references like "those", "them", "the above", "from the summary", "from the file".
- The user may write in ANY language or script — classify by meaning, not by matching specific words in any one language. Do not default to "general" just because the message isn't in English.
- Creation-vs-update disambiguation (any language): look at what action applies to the TASK RECORD ITSELF, not what the task's subject matter is about. An instruction to make/create/add a NEW task record → create_task, even when the task's content describes fixing/repairing something (e.g. "make me a task for fixing the login bug" is create_task — a new task is being made; the bug itself isn't what this message is fixing). An instruction to fix/change/update an EXISTING, already-referenced task (by ID or name) → update_task.
- A counting/quantity question ("how many", "how much", in any language) is always db_query, never list_tasks — even if it mentions a domain noun like "task". list_tasks is for "show me/list" requests, not counting questions.
- convert_request_to_task requires an EXPLICIT mention of an existing submitted "request" being turned into a task. A plain "create/make a task" instruction with no request mentioned at all is always create_task, never convert_request_to_task, regardless of how confident that might seem.

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

# Language-agnostic reference classification (see intent_schemas.py's
# _REFERENCE_TYPE docstring): the LLM does the linguistic understanding —
# in whatever language/script the user wrote — and reduces the task
# reference to one of four canonical, deterministically-resolvable shapes.
# The backend never inspects the reference text for language-specific
# ordinal/pronoun words; it only branches on reference_type (see
# ChatService._resolve_task_reference()).
_TASK_REFERENCE_CLASSIFICATION_RULES = """Classify reference_type:
- "all": the message does not target any single task at all — no ID, no title, no position, no "this/it" — it applies the action to the WHOLE task list at once (a bulk operation — e.g. "mark all tasks as done", "set everything to in_progress", or the equivalent meaning in any language, such as a word meaning "all"/"every"/"everything").
- "ordinal": the user refers to a task by its POSITION in a list they were just shown (e.g. "the second one", "the last one", in any language). Set ordinal_position:
  - 1 for "the first one", 2 for "the second one", 3 for "the third one", and so on (always positive, always counted from the start).
  - EXACTLY -1 for "the last one" — never a positive number, never a count of anything else in the message. If the user's word means "last"/"final" in whatever language they wrote, the answer is always the literal integer -1.
- "deictic": the user refers to "this"/"it"/"the current task" (in any language) with no explicit name or ID — they mean whatever task is currently on-screen or was most recently created/touched in this conversation.
- "explicit": the user named a specific task. Set task_reference to its ID number or a fragment of its title.
Do not try to match specific words — classify by MEANING, regardless of what language or script the message is written in."""

_UPDATE_TASK_SYSTEM = f"""You are a task-update assistant. Extract the update intent from the user's message.

{_TASK_REFERENCE_CLASSIFICATION_RULES}

Return ONLY a JSON object:
{{
  "reference_type": "all"|"ordinal"|"deictic"|"explicit",
  "task_reference": "string or null",
  "ordinal_position": "integer or null",
  "updates": {{
    "name": "string or null",
    "status": "todo|in_progress|done|pending_review or null",
    "due_date": "YYYY-MM-DD or null",
    "assignee_name": "string or null"
  }}
}}"""

_DELETE_TASK_SYSTEM = f"""Extract what the user wants to delete.

{_TASK_REFERENCE_CLASSIFICATION_RULES}

Use conversation history to resolve vague references like "that task" or "those".
Return ONLY JSON: {{"reference_type": "all"|"ordinal"|"deictic"|"explicit", "task_reference": "string or null", "ordinal_position": "integer or null"}}"""

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

_MANAGE_ISSUE_SYSTEM = """Extract what the user wants to do with an Issue.
Set "action" to "create" (a brand-new issue) or "update_status" (change an existing issue's status).
For "create": set title, team_name (which team this issue belongs to), and project_name if mentioned.
For "update_status": set issue_reference (the issue's ID number or a fragment of its title) and status ("open" or "resolved"); resolution_plan if the user described how it was/will be resolved.
Use conversation history to resolve vague references like "that issue".
Return ONLY JSON matching this shape:
{"action": "create"|"update_status", "title": "string or null", "team_name": "string or null", "project_name": "string or null", "issue_reference": "string or null", "status": "open"|"resolved"|null, "resolution_plan": "string or null"}"""

_MANAGE_ROCK_SYSTEM = """Extract what the user wants to do with a Rock (quarterly goal).
Set "action" to "create" (a brand-new rock) or "update_status" (change an existing rock's status).
For "create": set title, team_name, project_name if mentioned, and due_date (ISO YYYY-MM-DD) if mentioned.
For "update_status": set rock_reference (the rock's ID number or a fragment of its title) and status (one of: backlog, on_track, at_risk, off_track, done).
Use conversation history to resolve vague references like "that rock".
Return ONLY JSON matching this shape:
{"action": "create"|"update_status", "title": "string or null", "team_name": "string or null", "project_name": "string or null", "rock_reference": "string or null", "status": "backlog"|"on_track"|"at_risk"|"off_track"|"done"|null, "due_date": "YYYY-MM-DD or null"}"""

_RECORD_KPI_SYSTEM = """Extract a KPI value the user wants to record.
Set kpi_reference to the KPI's ID number or a fragment of its title, value to the numeric value to record, period_type to one of weekly/monthly/quarterly/yearly (default "weekly" if not stated), and note to any comment the user attached.
Return ONLY JSON: {"kpi_reference": "string", "value": <number>, "period_type": "weekly"|"monthly"|"quarterly"|"yearly", "note": "string or null"}"""

_SUBMIT_CLIENT_REQUEST_SYSTEM = """A client is submitting a new task request. Extract title (a short summary of what they want), description (more detail, if given), and project_name if they named a specific project.
Return ONLY JSON: {"title": "string", "description": "string or null", "project_name": "string or null"}"""

_MANAGE_MEETING_SYSTEM = """Extract what the user wants to do with a meeting.
Set "action" to "schedule" (a brand-new meeting) or "update" (reschedule, cancel, start, or end an existing one).
For "schedule": set title, scheduled_at (ISO 8601 datetime, e.g. "2026-03-15T15:00:00" — resolve relative dates like "tomorrow at 3pm" using today's date), duration_minutes (default 60 if not stated), team_name and/or project_name if mentioned, location if mentioned.
For "update": set meeting_reference (the meeting's ID number or a fragment of its title), scheduled_at if rescheduling, and status if the user wants to change its lifecycle state: "in_progress" for starting/beginning the meeting now, "completed" for ending/finishing/wrapping up the meeting, "cancelled" for cancelling it.
Use conversation history to resolve vague references like "that meeting".
Today's date: {today}
Return ONLY JSON matching this shape:
{"action": "schedule"|"update", "title": "string or null", "scheduled_at": "ISO datetime or null", "duration_minutes": <int or null>, "team_name": "string or null", "project_name": "string or null", "location": "string or null", "meeting_reference": "string or null", "status": "scheduled"|"in_progress"|"completed"|"cancelled"|null}"""

_CREATE_PROJECT_SYSTEM = """Extract the new project's name and description (if given) from the user's message.
Return ONLY JSON: {"name": "string", "description": "string or null"}"""

_CREATE_TEAM_SYSTEM = """Extract the new team's name, description (if given), and team_manager_name — the person who should manage this team (this is required; if the user didn't name anyone, set it to an empty string).
Return ONLY JSON: {"name": "string", "description": "string or null", "team_manager_name": "string"}"""

_CREATE_KNOWLEDGE_DOCUMENT_SYSTEM = """Extract a knowledge-base document the user wants to save. title is a short name for the document; content is the full text/body they want stored (use the actual substantive content they provided — if they pasted or described real policy/SOP text, use it verbatim, do not summarize it away). doc_type is a short category like "sop", "policy", "faq", "runbook", or "general" if unclear. tags is a list of a few relevant keyword tags (can be empty).
Return ONLY JSON: {"title": "string", "content": "string", "doc_type": "string", "tags": ["string", ...]}"""

_MANAGE_PROJECT_SYSTEM = """Extract what the user wants to update about an EXISTING project.
Set project_reference to the project's ID number or a fragment of its name.
Set name if they want to rename it, description if they want to change its description, and status (one of: active, paused, completed, cancelled) if they want to change its status — "archive" means status="cancelled".
Use conversation history to resolve vague references like "that project".
Return ONLY JSON: {"project_reference": "string", "name": "string or null", "description": "string or null", "status": "active"|"paused"|"completed"|"cancelled"|null}"""

_MANAGE_TEAM_SYSTEM = """Extract what the user wants to do with an EXISTING team.
Set "action" to "update" (rename/change description) or "reassign_manager" (change who manages the team).
Set team_reference to the team's ID number or a fragment of its name.
For "update": set name and/or description to the new value(s).
For "reassign_manager": set new_manager_name to the person who should now manage the team.
Use conversation history to resolve vague references like "that team".
Return ONLY JSON: {"action": "update"|"reassign_manager", "team_reference": "string", "name": "string or null", "description": "string or null", "new_manager_name": "string or null"}"""

_GENERATE_REPORT_SYSTEM = """Extract a project status report request.
Set project_reference to the project's ID number or a fragment of its name.
Set report_type to one of: weekly, monthly, client, team_performance (default "monthly" if not stated).
Set title to a short title for the report (if not stated, use something like "<project name> <report_type> report").
Return ONLY JSON: {"project_reference": "string", "report_type": "weekly"|"monthly"|"client"|"team_performance", "title": "string"}"""

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
- my_scoreboard: "what's my score", "how am I doing this month", "my performance", "show my scoreboard" — set period to this_week/this_month/this_quarter/this_year (default this_month) based on what timeframe, if any, the user mentioned
- team_scoreboard: a MANAGER-level request for a specific TEAM's performance scoreboard/score — e.g. "how is team Alpha performing", "team Beta's scoreboard", "what's team Alpha's score this month" (requires team_name; distinct from team_workload which is about task counts, not performance scoring)
- org_scoreboard: a request for the company-wide/organization-wide employee leaderboard or ranking — e.g. "show the company leaderboard", "who are the top performers", "organization scoreboard", "employee rankings this quarter"
- search_everything: a broad/vague lookup that doesn't clearly name one specific domain (task/rock/issue/project/meeting) — e.g. "find anything about the Q2 launch", "search for website redesign", "what do we have about the client onboarding"
- search_knowledge: the user is asking about a DOCUMENT/SOP/POLICY/RUNBOOK/FAQ in the knowledge base, not live task/project data — e.g. "what does our SOP say about refunds", "find the onboarding doc", "search the knowledge base for X", "do we have documentation on Y"

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

The user may write in ANY language or script. Extract the same
sub_intent/parameters regardless of language/script — classify by meaning,
not by matching specific words in any one language.
"""


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
        self._org_timezone_cache: str | None = None
        # Set per-turn by handle_message(); defaults to None so any handler
        # called directly (e.g. in tests, or a future internal call site
        # that bypasses handle_message) degrades safely to "no page context
        # available" rather than an AttributeError.
        self._page_context: PageContext | None = None
        # Captured once (see _safe_user_id below), not re-derived from
        # `user.id` at every ToolContext construction — see
        # tools/registry.py's ToolContext.user_id docstring for exactly why
        # that matters (a rollback anywhere in this turn expires every ORM
        # object still attached to self._db, including `user`; re-reading
        # `user.id` afterward crashes with MissingGreenlet).
        self._user_id: int | None = None
        # Multi-goal intermediate-result propagation (architecture item 11,
        # generalized): the Structured Planner (planner.py) splits one
        # message into several independent steps run in sequence through
        # this same ChatService instance — "create a task for the launch
        # and then assign it to Sarah" becomes step 1 (create) + step 2
        # (reassign "it"). Without this, step 2 has no way to know WHICH
        # record step 1 just touched.
        #
        # Generalized beyond a single "_last_created_task_id" slot (its
        # original, narrower form): every write handler below calls
        # _record_step_result() after a successful tool call, for every
        # entity type (task/issue/rock/meeting/project/team/kpi/client
        # request), not just tasks — so "create an issue... then resolve
        # it" or "schedule a meeting... then cancel it" work the same way
        # "create a task... then reassign it" already did. Keyed by entity
        # type since a deictic "it" always refers to whatever kind of
        # record the CURRENT handler is asking about (an update_issue
        # handler resolving "it" only cares about the most recent issue,
        # not the most recent task) — see _resolve_deictic_entity_id().
        self._step_results: dict[str, int] = {}

    def _record_step_result(self, entity_type: str, entity_id: int) -> None:
        self._step_results[entity_type] = entity_id

    def _safe_user_id(self, user: User) -> int:
        if self._user_id is None:
            self._user_id = user.id
        return self._user_id

    async def _org_today(self) -> date:
        """Architecture item 9 — "timezone-aware temporal resolution": the
        organization's own local calendar day, not the server process's
        local clock. Every "today"/overdue/due-soon boundary in this file
        used to call date.today() directly, which is silently wrong for any
        organization not in the same timezone as wherever this process
        happens to be deployed — near midnight, "today" and "overdue" could
        disagree with what the org's own clock says by a full day. Falls
        back to UTC if the org has no timezone set or it's not a valid IANA
        name (never raises — a bad timezone value must not break every chat
        turn)."""
        if self._org_timezone_cache is None:
            result = await self._db.execute(
                select(Organization.timezone).where(Organization.id == self._org_id)
            )
            self._org_timezone_cache = result.scalar_one_or_none() or "UTC"
        try:
            tz = ZoneInfo(self._org_timezone_cache)
        except ZoneInfoNotFoundError:
            logger.warning("Unknown organization timezone %r — falling back to UTC", self._org_timezone_cache)
            tz = ZoneInfo("UTC")
        return datetime.now(tz).date()

    async def _today_str(self) -> str:
        return (await self._org_today()).isoformat()

    # ─── Public entry point ───────────────────────────────────────────────────

    async def handle_message(
        self,
        *,
        user: User,
        message: str,
        session_id: int | None,
        org_role: str = TEAM_MEMBER,
        file_context: dict | None = None,
        page_context: PageContext | None = None,
    ) -> ChatMessageResponse:
        """
        file_context (optional): {"filename": str, "text": str, "size_bytes": int}
        When provided the file content is injected into the LLM prompt and the
        stored user message is prefixed with a [📎 filename] badge.

        page_context (optional, architecture item 9): what the user was
        looking at when they sent this message (e.g. a task's detail page)
        — used only to resolve deictic references ("mark this done") when
        the message itself gives no explicit name/ID; see
        _resolve_deictic_entity_id(). Stored on self like _trace_id — safe
        for the same reason (one ChatService instance per HTTP request).
        """
        self._page_context = page_context
        # Observability (spec Section 49) — one trace_id per turn, threaded
        # through log lines and audit rows so every tool call made while
        # handling this single message can be correlated.
        # Stored on self rather than threaded through every method signature —
        # safe because a fresh ChatService instance is created per HTTP
        # request (see app/api/routes/chat.py), so this never leaks across turns.
        self._trace_id = str(uuid.uuid4())
        # Every log line for the rest of this turn — in this file and in
        # app.services.copilot.* — is auto-tagged with these ids by
        # ChatContextFilter (see app/core/log_context.py), including the
        # fire-and-forget asyncio.create_task(...) work below that keeps
        # running after the response is sent. No other call site needs to
        # change for this to take effect.
        # Captured as a plain int immediately (via the existing _safe_user_id
        # cache) rather than read as `user.id` throughout this method — see
        # the matching comment below on `session_id` for exactly why: a
        # tool call triggered by any step of this request can commit()/
        # rollback() self._db, and `user` is attached to that same session
        # (it was loaded by the same request-scoped AsyncSession the auth
        # dependency and this service share), so it's just as exposed to
        # the MissingGreenlet-on-expired-attribute crash as `session` is.
        user_id = self._safe_user_id(user)
        set_chat_context(trace_id=self._trace_id, user_id=user_id, org_id=self._org_id)
        logger.info("Chat turn started: query=%r (session=%s)", message, session_id)

        # 1. Get or create session
        session = await self._get_or_create_session(user, session_id, message)
        # Captured as plain ints/bools immediately, before any write below
        # can run — a tool call triggered by ANY later step (this method
        # handles a multi-goal turn as a loop of steps) may commit() or
        # rollback() self._db deep inside run_tool()/the Transaction
        # Coordinator, which expires every ORM object already attached to
        # that session, including `session` itself. Synchronously
        # re-reading an attribute like `session.id` off an expired instance
        # after that point tries to lazily reload it, which fails outside
        # the async greenlet with MissingGreenlet — this is the same
        # pitfall documented on ToolContext.user_id; the fix is identical:
        # capture the plain value once, early, and never read the ORM
        # object's attributes again in this method.
        session_id = session.id
        had_no_title = session.title is None
        set_chat_context(session_id=session_id)

        # 2. Build stored message (with file badge when applicable)
        stored_user_message = _build_stored_message(message, file_context)
        user_msg = await self._chat_repo.add_message(session_id, "user", stored_user_message)

        # 3. Build conversation history for context
        history = await self._chat_repo.get_session_messages(session_id, limit=20)
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
            assistant_msg = await self._chat_repo.add_message(session_id, "assistant", reply)
            if len(history) <= 2 and had_no_title:
                await self._auto_title_session(session_id, file_context["filename"])
            await self._chat_repo.touch_session(session_id)
            return ChatMessageResponse(
                session_id=session_id,
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
                history_text, org_role, session_id, user,
            )
            step_replies.append(step_reply)
            actions.extend(step_actions)

        reply = state_notice + (
            step_replies[0] if len(step_replies) == 1
            else "\n\n".join(f"**{i}.** {r}" for i, r in enumerate(step_replies, 1))
        )

        logger.info("Chat turn reply: %r", reply)

        # 8. Persist assistant reply
        assistant_msg = await self._chat_repo.add_message(session_id, "assistant", reply)

        # 9. Auto-title session after first exchange (fire-and-forget — doesn't block response).
        if len(history) <= 2 and had_no_title:
            title_hint = file_context["filename"] if file_context else message
            asyncio.create_task(self._auto_title_session(session_id, title_hint))

        # 9b. Roll the session's topic summary forward (best-effort, same
        # fire-and-forget contract as auto-titling — never blocks the reply).
        if message.strip():
            asyncio.create_task(topics.update_topic(self._llm, session_id, message, reply))
            asyncio.create_task(
                memory.maybe_learn_preference(self._llm, org_id=self._org_id, user_id=user_id, message=message)
            )

        await self._chat_repo.touch_session(session_id)

        return ChatMessageResponse(
            session_id=session_id,
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
        except Exception:
            # logger.exception() already includes the full traceback — that
            # plus the trace_id/session context stamped on every line (see
            # log_context.py) and the intent/query below is normally enough
            # to jump straight to the failing handler and the input that
            # broke it, without needing to reproduce the bug interactively.
            logger.exception("Error handling intent=%s for query=%r", intent, step_message)
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
                capability="session_title",
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
            # Non-critical (the session just keeps its default title) but
            # not silent — was a bare `pass` that hid real LLM/DB failures
            # here from the terminal entirely.
            logger.warning("Auto-title generation failed for session=%s", session_id, exc_info=True)

    # ─── Intent detection ─────────────────────────────────────────────────────

    async def _detect_intent(self, message: str, history: str) -> tuple[str, float]:
        prompt = f"Previous conversation:\n{history}\n\nUser message: {message}" if history else message
        try:
            # generate_structured() (architecture item 4/5 — Structured LLM
            # Gateway + structured semantic understanding) validates against
            # IntentDetectionResult, not just "is this JSON": an intent
            # value outside the closed Literal set, or an out-of-range/
            # non-numeric confidence, triggers the same repair-retry as
            # malformed JSON — instead of a typo'd/invented intent string
            # silently falling through every `==` in _route() straight to
            # _handle_general with zero signal anything went wrong (which is
            # exactly what the old bare-dict version of this call allowed).
            # confidence's own default (0.0 when omitted, never silently
            # 1.0) is documented on IntentDetectionResult itself — see
            # architecture Section 13's non-negotiable rule.
            result = await self._llm.generate_structured(
                system_prompt=_INTENT_SYSTEM,
                user_prompt=prompt,
                schema=IntentDetectionResult,
                temperature=0.0,
                capability="intent_detection",
            )
            return result.intent, result.confidence
        except Exception:
            # exc_info=True — without a traceback here, "intent detection
            # failed" tells you nothing about *why* (bad LLM JSON vs.
            # provider timeout vs. a real code bug) when scanning logs.
            logger.warning("Intent detection failed for query=%r — defaulting to general", message, exc_info=True)
            return INTENT_GENERAL, 0.0

    # Ambiguity Engine (spec Section 15, bounded) — below this confidence, a
    # mutation-risk intent is not routed automatically; the user is asked to
    # confirm what they meant instead of the assistant silently guessing.
    _AMBIGUITY_CONFIDENCE_FLOOR = 0.55
    _AMBIGUITY_GATED_INTENTS = {INTENT_CREATE_TASK, INTENT_UPDATE_TASK, INTENT_DELETE_TASK, INTENT_CONVERT_REQUEST}

    # ─── Router ───────────────────────────────────────────────────────────────

    # Coarse, fast pre-check (architecture Section 4.5/6) — avoids burning an
    # LLM extraction call when we already know the role can't do this at
    # all. This is a UX optimization ONLY; it is not the authority. The
    # actual authorization decision is made exactly once, centrally, by
    # tools.registry.check_write_authorized() inside each handler/tool —
    # this map just names which tool's rule applies per intent so this
    # pre-check can never drift out of sync with the tools' own rules
    # (which is exactly how the old create_task bypass happened: this used
    # to be a separately-hardcoded role set that simply forgot CLIENT).
    _INTENT_REPRESENTATIVE_TOOL = {
        INTENT_CREATE_TASK: "create_task",
        INTENT_UPDATE_TASK: "update_task_field",
        INTENT_DELETE_TASK: "delete_task_single",
        INTENT_CONVERT_REQUEST: "convert_client_request_to_task",
        # Domain buildout — coarse pre-check representative tool per new
        # intent (see the class-level comment above this map's first use
        # for why this must stay in sync with the tools' own allowed_roles
        # rather than being a separately-hardcoded role set).
        INTENT_MANAGE_ISSUE: "create_issue",
        INTENT_MANAGE_ROCK: "create_rock",
        INTENT_RECORD_KPI: "record_kpi_value",
        INTENT_SUBMIT_CLIENT_REQUEST: "submit_client_request",
        INTENT_MANAGE_MEETING: "schedule_meeting",
        INTENT_CREATE_PROJECT: "create_project",
        INTENT_CREATE_TEAM: "create_team",
        INTENT_MANAGE_KNOWLEDGE: "create_knowledge_document",
        INTENT_MANAGE_PROJECT: "update_project",
        INTENT_MANAGE_TEAM: "update_team",  # coarse pre-check only — reassign_manager re-checked at its own entry point below
        INTENT_GENERATE_REPORT: "generate_project_report",
    }

    async def _route(
        self, intent: str, user: User, message: str, history: str, org_role: str = TEAM_MEMBER,
        session_id: int | None = None,
    ) -> tuple[str, list[ChatAction]]:
        representative_tool = self._INTENT_REPRESENTATIVE_TOOL.get(intent)
        if representative_tool is not None:
            refusal = check_write_authorized(org_role=org_role, tool_name=representative_tool)
            if refusal is not None:
                return refusal, []
        if intent == INTENT_CREATE_TASK:
            return await self._handle_create_task(user, message, history, org_role, session_id)
        if intent == INTENT_LIST_TASKS:
            return await self._handle_list_tasks(user, message, history, org_role, session_id)
        if intent == INTENT_UPDATE_TASK:
            return await self._handle_update_task(user, message, history, session_id, org_role)
        if intent == INTENT_DELETE_TASK:
            return await self._handle_delete_task(user, message, history, session_id, org_role)
        if intent == INTENT_CONVERT_REQUEST:
            return await self._handle_convert_request(user, message, history, session_id, org_role)
        if intent == INTENT_ANALYZE_TEXT:
            return await self._handle_analyze_text(user, message, history, org_role, session_id)
        if intent == INTENT_DB_QUERY:
            return await self._handle_db_query(user, message, history, org_role)
        if intent == INTENT_MANAGE_ISSUE:
            return await self._handle_manage_issue(user, message, history, org_role, session_id)
        if intent == INTENT_MANAGE_ROCK:
            return await self._handle_manage_rock(user, message, history, org_role, session_id)
        if intent == INTENT_RECORD_KPI:
            return await self._handle_record_kpi(user, message, history, org_role, session_id)
        if intent == INTENT_SUBMIT_CLIENT_REQUEST:
            return await self._handle_submit_client_request(user, message, history, org_role, session_id)
        if intent == INTENT_MANAGE_MEETING:
            return await self._handle_manage_meeting(user, message, history, org_role, session_id)
        if intent == INTENT_CREATE_PROJECT:
            return await self._handle_create_project(user, message, history, org_role, session_id)
        if intent == INTENT_CREATE_TEAM:
            return await self._handle_create_team(user, message, history, org_role, session_id)
        if intent == INTENT_MANAGE_KNOWLEDGE:
            return await self._handle_manage_knowledge(user, message, history, org_role, session_id)
        if intent == INTENT_MANAGE_PROJECT:
            return await self._handle_manage_project(user, message, history, org_role, session_id)
        if intent == INTENT_MANAGE_TEAM:
            return await self._handle_manage_team(user, message, history, org_role, session_id)
        if intent == INTENT_GENERATE_REPORT:
            return await self._handle_generate_report(user, message, history, org_role, session_id)
        return await self._handle_general(user, message, history, session_id)

    # ─── Create task ──────────────────────────────────────────────────────────

    async def _handle_create_task(
        self, user: User, message: str, history: str = "",
        org_role: str = TEAM_MEMBER, session_id: int | None = None,
    ) -> tuple[str, list[ChatAction]]:
        users_block = await self._build_users_block()
        system = (
            _CREATE_TASK_SYSTEM
            .replace("{users_block}", users_block)
            .replace("{today}", await self._today_str())
        )
        user_prompt = (
            f"Conversation history:\n{history}\n\nUser instruction: {message}"
            if history else message
        )
        data = await self._llm.generate_json(
            system_prompt=system,
            user_prompt=user_prompt,
            temperature=0.1,
            capability="task_extraction",
        )
        raw_tasks: list[dict] = data.get("tasks", [])

        if not raw_tasks:
            logger.info("create_task: LLM extracted no tasks from raw output=%r", data)
            return "I couldn't extract any tasks from that. Could you be more specific about what needs to be done?", []

        # Understanding/resolution stays here (name/date parsing, assignee
        # /project/team name -> id resolution via the reference resolver) —
        # the actual mutation is now delegated to the Domain Tool Registry
        # (architecture Section 4.5/25), which is the single place that
        # validates the schema, checks centralized authorization, and
        # verifies the write afterward. This tool used to call
        # self._task_repo.create() directly with NO permission check at
        # all — see tools/registry.py's module docstring for the bug that
        # closed.
        tool_items = []
        resolution_warnings: list[str] = []
        for raw in raw_tasks:
            name = (raw.get("name") or "").strip()
            if not name:
                continue
            # No-silent-fallback (architecture item 6): an assignee/project/
            # team name that was actually mentioned but doesn't resolve is
            # surfaced to the user, never silently swapped for `fallback`
            # (self-assignment) or dropped without a trace.
            assignee_id, assignee_warning = await self._resolve_user_id(raw.get("assignee_name"), fallback=user.id)
            project_id, project_warning = await self._resolve_project_id(raw.get("project_name"))
            team_id, team_warning = await self._resolve_team_id(raw.get("team_name"))
            for warning in (assignee_warning, project_warning, team_warning):
                if warning:
                    resolution_warnings.append(f'"{name}": {warning}')
            tool_items.append({
                "name": name,
                "start_date": _parse_date(raw.get("start_date")),
                "due_date": _parse_date(raw.get("due_date")),
                "assignee_id": assignee_id,
                "project_id": project_id,
                "team_id": team_id,
                "status": raw.get("status", "todo"),
            })

        if not tool_items:
            logger.info("create_task: every extracted task lacked a usable name, raw_tasks=%r", raw_tasks)
            return "I understood you want to create tasks but couldn't parse the details. Could you provide more specific task names?", []

        ctx = ToolContext(
            db=self._db, org_id=self._org_id, org_role=org_role, user=user, user_id=self._safe_user_id(user),
            session_id=session_id, trace_id=getattr(self, "_trace_id", None),
        )
        tool_result = await run_tool("create_task", {"tasks": tool_items}, ctx)
        if not tool_result.ok:
            return tool_result.message, []

        # Multi-goal intermediate-result propagation (architecture item 11)
        # — a later step in this same turn ("...and then assign it to
        # Sarah") can resolve "it" against whatever this step just created.
        created_ids = (tool_result.data or {}).get("task_ids", [])
        if created_ids:
            self._record_step_result("task", created_ids[-1])

        # Notifications are a side effect of ChatService's own workflow
        # (assignment emails), kept here rather than inside the tool so the
        # tool stays a pure data-mutation+verification unit.
        for task_id in created_ids:
            task = await self._task_repo.get_by_id(task_id)
            if task is not None:
                await self._notify_assigned(task, assigned_by=user)

        reply = tool_result.message
        if resolution_warnings:
            reply += "\n\n⚠️ " + " ".join(resolution_warnings)
        return reply, tool_result.actions

    # ─── List tasks ───────────────────────────────────────────────────────────

    async def _handle_list_tasks(
        self, user: User, message: str, history: str = "", org_role: str = TEAM_MEMBER,
        session_id: int | None = None,
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
            capability="list_tasks_summary",
        )

        # Previous result-set memory (architecture item 8) — the same
        # ordered task list just shown to the LLM (and, per its system
        # prompt, referenced in its bullet points) becomes this session's
        # positional-follow-up context, so "mark the second one done" can
        # resolve deterministically on the next turn.
        await self._store_result_set(session_id, "task", [t.id for t in tasks[:50]])

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

        # Defense-in-depth — _route()'s pre-check already covers this intent
        # via "update_task_field", and every task-update tool this handler
        # can reach (update_task_field/reassign_task/update_task_bulk) shares
        # the same allowed_roles set, so this second check is redundant on
        # the happy path but guards against this handler ever being called
        # from a new call site that skips _route().
        refusal = check_write_authorized(org_role=org_role, tool_name="update_task_field")
        if refusal is not None:
            return refusal, []

        _can_see_all = org_role in {OWNER, ADMIN, TEAM_MANAGER}

        user_prompt = (
            f"Conversation history:\n{history}\n\nUser instruction: {message}"
            if history else message
        )
        extraction = await self._llm.generate_structured(
            system_prompt=_UPDATE_TASK_SYSTEM, user_prompt=user_prompt, schema=UpdateTaskExtraction, temperature=0.1,
            capability="update_task_extraction",
        )
        updates_raw = extraction.updates

        # Build the update payload from LLM output
        update_payload: dict = {}
        if updates_raw.name:
            update_payload["name"] = updates_raw.name
        if updates_raw.status:
            update_payload["status"] = updates_raw.status
        if updates_raw.due_date:
            update_payload["due_date"] = _parse_date(updates_raw.due_date)
        if updates_raw.assignee_name:
            users = await self._get_users()
            resolution = reference_resolver.resolve_by_name(
                users, updates_raw.assignee_name, lambda u: u.full_name, lambda u: u.id,
            )
            if resolution.status == ResolutionStatus.AMBIGUOUS:
                return resolution.clarification_message_for("person"), []
            if resolution.entity is not None:
                update_payload["assignee_id"] = resolution.entity.id

        if not update_payload:
            logger.info("update_task: no usable fields in updates=%r (extraction=%r)", updates_raw, extraction)
            return "I understood you want to update a task but couldn't determine what to change. Could you be more specific?", []

        # ── Bulk update — always previewed and confirmed (risk R4), never
        #    applied immediately, regardless of which fields are touched. ──
        if extraction.reference_type == "all":
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

        # ── Single task update — canonical, language-agnostic resolution
        #    (see _resolve_task_reference()'s docstring). ──
        candidate_tasks = await self._task_repo.list_all() if _can_see_all else await self._task_repo.list_for_assignee(user.id)
        task, clarification = await self._resolve_task_reference(
            session_id=session_id, reference_type=extraction.reference_type,
            task_reference=extraction.task_reference, ordinal_position=extraction.ordinal_position,
            candidate_tasks=candidate_tasks,
        )
        if clarification is not None:
            return clarification, []

        if not task:
            logger.info(
                "update_task: no task matched reference_type=%r task_reference=%r ordinal_position=%r among %d candidate(s)",
                extraction.reference_type, extraction.task_reference, extraction.ordinal_position, len(candidate_tasks),
            )
            return (
                f'I couldn\'t find a task matching "{extraction.task_reference or extraction.reference_type}". '
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

        # Simple field edits (status/name/due_date, no reassignment) go
        # through the Domain Tool Registry — schema validation + centralized
        # authorization + post-write verification + audit, none of which the
        # old direct `self._task_repo.update()` call here provided.
        ctx = ToolContext(
            db=self._db, org_id=self._org_id, org_role=org_role, user=user, user_id=self._safe_user_id(user),
            session_id=session_id, trace_id=getattr(self, "_trace_id", None),
        )
        tool_params = {"task_id": task.id, **{k: v for k, v in update_payload.items() if k != "assignee_id"}}
        tool_result = await run_tool("update_task_field", tool_params, ctx)
        return tool_result.message, tool_result.actions

    # ─── Delete task ──────────────────────────────────────────────────────────

    async def _handle_delete_task(
        self, user: User, message: str, history: str = "", session_id: int | None = None,
        org_role: str = TEAM_MEMBER,
    ) -> tuple[str, list[ChatAction]]:
        from app.core.org_roles import ADMIN, OWNER, TEAM_MANAGER

        # Centralized authorization (single source of truth — see
        # tools/registry.py). Replaces the old handler-local ad hoc role
        # check, which is exactly the pattern that let the create_task
        # bypass happen in the first place.
        refusal = check_write_authorized(org_role=org_role, tool_name="delete_task_single")
        if refusal is not None:
            return refusal, []

        user_prompt = (
            f"Conversation history:\n{history}\n\nUser instruction: {message}"
            if history else message
        )
        extraction = await self._llm.generate_structured(
            system_prompt=_DELETE_TASK_SYSTEM, user_prompt=user_prompt, schema=DeleteTaskExtraction, temperature=0.0,
            capability="delete_task_extraction",
        )

        # ── Bulk delete — blocked outright (risk R7), never executed from
        #    chat regardless of confirmation. A chat confirmation is too thin
        #    a safeguard for wiping every task in the organization at once. ──
        if extraction.reference_type == "all":
            return (
                "Mass-deleting every task isn't available through the assistant, for safety. "
                "Please delete tasks individually from the Tasks page, or ask an admin.",
                [],
            )

        # ── Single task delete — canonical, language-agnostic resolution
        #    (see _resolve_task_reference()'s docstring). ──
        can_see_all = org_role in {OWNER, ADMIN, TEAM_MANAGER}
        candidate_tasks = await self._task_repo.list_all() if can_see_all else await self._task_repo.list_for_assignee(user.id)
        task, clarification = await self._resolve_task_reference(
            session_id=session_id, reference_type=extraction.reference_type,
            task_reference=extraction.task_reference, ordinal_position=extraction.ordinal_position,
            candidate_tasks=candidate_tasks,
        )
        if clarification is not None:
            return clarification, []

        if not task:
            logger.info(
                "delete_task: no task matched reference_type=%r task_reference=%r ordinal_position=%r",
                extraction.reference_type, extraction.task_reference, extraction.ordinal_position,
            )
            return (
                f'I couldn\'t find a task matching "{extraction.task_reference or extraction.reference_type}". '
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
        refusal = check_write_authorized(org_role=org_role, tool_name="convert_client_request_to_task")
        if refusal is not None:
            return refusal, []

        user_prompt = (
            f"Conversation history:\n{history}\n\nUser instruction: {message}"
            if history else message
        )
        data = await self._llm.generate_json(
            system_prompt=(
                "Extract which client task request the user wants converted into a real task.\n"
                "Set request_reference to the request ID number if given, otherwise a fragment of its title.\n"
                'Return ONLY JSON: {"request_reference": "string"}'
            ),
            user_prompt=user_prompt,
            temperature=0.0,
            capability="convert_request_extraction",
        )
        ref = (data.get("request_reference") or "").strip()
        if not ref:
            logger.info("convert_request: LLM extracted no request_reference, raw=%r", data)
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
            logger.info("convert_request: no pending request matched reference=%r among %d candidate(s)", ref, len(pending))
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
        # SECURITY (architecture Section 4.2 / non-negotiable rule #2):
        # this MUST be org-scoped. UserRepository.list_all() is a genuinely
        # global, cross-tenant query with no organization filter at all —
        # every direct/indirect caller of _get_users() (assignee-name
        # resolution, the "Known system users" LLM prompt block, user
        # search) used to see and could resolve users from every
        # organization on the platform, not just the caller's own.
        if self._users_cache is None:
            self._users_cache = await self._user_repo.list_by_org(self._org_id)
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

    # ─── Domain buildout: Issues / Rocks / KPI / Client Requests ───────────────
    # Each handler follows the exact same shape as the task handlers above:
    # structured extraction (generate_structured) → entity resolution (no
    # silent fallback — an unresolved team/project/issue/rock/KPI reference
    # is surfaced, never guessed) → the Domain Tool Registry (RBAC/ABAC/
    # verification/audit already enforced centrally by run_tool()) →
    # grounded response built from the tool's own verified result.

    async def _handle_manage_issue(
        self, user: User, message: str, history: str = "",
        org_role: str = TEAM_MEMBER, session_id: int | None = None,
    ) -> tuple[str, list[ChatAction]]:
        user_prompt = f"Conversation history:\n{history}\n\nUser instruction: {message}" if history else message
        extraction = await self._llm.generate_structured(
            system_prompt=_MANAGE_ISSUE_SYSTEM, user_prompt=user_prompt, schema=ManageIssueExtraction, temperature=0.1,
            capability="issue_extraction",
        )
        ctx = ToolContext(
            db=self._db, org_id=self._org_id, org_role=org_role, user=user, user_id=self._safe_user_id(user),
            session_id=session_id, trace_id=getattr(self, "_trace_id", None),
        )

        if extraction.action == "create":
            if not extraction.title:
                return "What should the issue be called?", []
            team_id, team_warning = await self._resolve_team_id(extraction.team_name)
            if team_warning:
                return team_warning, []
            if team_id is None:
                return "Which team is this issue for?", []
            project_id, project_warning = await self._resolve_project_id(extraction.project_name)
            if project_warning:
                return project_warning, []
            result = await run_tool("create_issue", {"title": extraction.title, "team_id": team_id, "project_id": project_id}, ctx)
            if result.ok and result.data and result.data.get("issue_id"):
                self._record_step_result("issue", result.data["issue_id"])
            return result.message, result.actions

        # update_status
        if not extraction.issue_reference:
            return "Which issue would you like to update?", []
        issue_id = await self._resolve_reference_id(
            extraction.issue_reference, await self._query_issues(user=user, org_role=org_role),
            name_fn=lambda i: i.title, label="issue",
        )
        if isinstance(issue_id, tuple):  # (None, message) — unresolved
            return issue_id[1], []
        if not extraction.status:
            return "What status should the issue have — open or resolved?", []
        result = await run_tool(
            "update_issue_status",
            {"issue_id": issue_id, "status": extraction.status, "resolution_plan": extraction.resolution_plan},
            ctx,
        )
        return result.message, result.actions

    async def _handle_manage_rock(
        self, user: User, message: str, history: str = "",
        org_role: str = TEAM_MEMBER, session_id: int | None = None,
    ) -> tuple[str, list[ChatAction]]:
        user_prompt = f"Conversation history:\n{history}\n\nUser instruction: {message}" if history else message
        extraction = await self._llm.generate_structured(
            system_prompt=_MANAGE_ROCK_SYSTEM, user_prompt=user_prompt, schema=ManageRockExtraction, temperature=0.1,
            capability="rock_extraction",
        )
        ctx = ToolContext(
            db=self._db, org_id=self._org_id, org_role=org_role, user=user, user_id=self._safe_user_id(user),
            session_id=session_id, trace_id=getattr(self, "_trace_id", None),
        )

        if extraction.action == "create":
            if not extraction.title:
                return "What should the rock be called?", []
            team_id, team_warning = await self._resolve_team_id(extraction.team_name)
            if team_warning:
                return team_warning, []
            if team_id is None:
                return "Which team is this rock for?", []
            project_id, project_warning = await self._resolve_project_id(extraction.project_name)
            if project_warning:
                return project_warning, []
            result = await run_tool(
                "create_rock",
                {"title": extraction.title, "team_id": team_id, "project_id": project_id, "due_date": _parse_date(extraction.due_date)},
                ctx,
            )
            if result.ok and result.data and result.data.get("rock_id"):
                self._record_step_result("rock", result.data["rock_id"])
            return result.message, result.actions

        # update_status
        if not extraction.rock_reference:
            return "Which rock would you like to update?", []
        rock_id = await self._resolve_reference_id(
            extraction.rock_reference, await self._query_rocks(user=user, org_role=org_role),
            name_fn=lambda r: r.title, label="rock",
        )
        if isinstance(rock_id, tuple):
            return rock_id[1], []
        if not extraction.status:
            return "What status should the rock have?", []
        result = await run_tool("update_rock_status", {"rock_id": rock_id, "status": extraction.status}, ctx)
        return result.message, result.actions

    async def _handle_record_kpi(
        self, user: User, message: str, history: str = "",
        org_role: str = TEAM_MEMBER, session_id: int | None = None,
    ) -> tuple[str, list[ChatAction]]:
        user_prompt = f"Conversation history:\n{history}\n\nUser instruction: {message}" if history else message
        extraction = await self._llm.generate_structured(
            system_prompt=_RECORD_KPI_SYSTEM, user_prompt=user_prompt, schema=RecordKpiExtraction, temperature=0.0,
            capability="kpi_extraction",
        )
        kpis = await self._query_kpis(user=user, org_role=org_role)
        kpi_id = await self._resolve_reference_id(
            extraction.kpi_reference, kpis, name_fn=lambda k: k.title, label="KPI",
        )
        if isinstance(kpi_id, tuple):
            return kpi_id[1], []

        ctx = ToolContext(
            db=self._db, org_id=self._org_id, org_role=org_role, user=user, user_id=self._safe_user_id(user),
            session_id=session_id, trace_id=getattr(self, "_trace_id", None),
        )
        result = await run_tool(
            "record_kpi_value",
            {"kpi_id": kpi_id, "value": extraction.value, "period_type": extraction.period_type, "note": extraction.note},
            ctx,
        )
        if result.ok and result.data and result.data.get("kpi_id"):
            self._record_step_result("kpi", result.data["kpi_id"])
        return result.message, result.actions

    async def _handle_submit_client_request(
        self, user: User, message: str, history: str = "",
        org_role: str = TEAM_MEMBER, session_id: int | None = None,
    ) -> tuple[str, list[ChatAction]]:
        user_prompt = f"Conversation history:\n{history}\n\nUser instruction: {message}" if history else message
        extraction = await self._llm.generate_structured(
            system_prompt=_SUBMIT_CLIENT_REQUEST_SYSTEM, user_prompt=user_prompt, schema=SubmitClientRequestExtraction, temperature=0.1,
            capability="client_request_extraction",
        )
        if not extraction.title:
            return "What would you like to request?", []

        project_id: int | None = None
        if extraction.project_name:
            project_id, project_warning = await self._resolve_project_id(extraction.project_name)
            if project_warning:
                return project_warning, []
        if project_id is None:
            # No silent fallback (item 6) — a client with exactly one
            # accessible project could reasonably default to it, but
            # guessing among several would be exactly the class of bug this
            # session already fixed once; ask instead.
            projects = await self._get_projects()
            client_projects = [p for p in projects if await self._project_repo.is_member(p.id, user.id)]
            if len(client_projects) == 1:
                project_id = client_projects[0].id
            elif not client_projects:
                return "I couldn't find a project you're a member of to submit this request for.", []
            else:
                return "Which project is this request for?", []

        ctx = ToolContext(
            db=self._db, org_id=self._org_id, org_role=org_role, user=user, user_id=self._safe_user_id(user),
            session_id=session_id, trace_id=getattr(self, "_trace_id", None),
        )
        result = await run_tool(
            "submit_client_request",
            {"title": extraction.title, "description": extraction.description, "project_id": project_id},
            ctx,
        )
        if result.ok and result.data and result.data.get("request_id"):
            self._record_step_result("client_request", result.data["request_id"])
        return result.message, result.actions

    async def _handle_manage_meeting(
        self, user: User, message: str, history: str = "",
        org_role: str = TEAM_MEMBER, session_id: int | None = None,
    ) -> tuple[str, list[ChatAction]]:
        user_prompt = f"Conversation history:\n{history}\n\nUser instruction: {message}" if history else message
        system = _MANAGE_MEETING_SYSTEM.replace("{today}", await self._today_str())
        extraction = await self._llm.generate_structured(
            system_prompt=system, user_prompt=user_prompt, schema=ManageMeetingExtraction, temperature=0.1,
            capability="meeting_extraction",
        )
        ctx = ToolContext(
            db=self._db, org_id=self._org_id, org_role=org_role, user=user, user_id=self._safe_user_id(user),
            session_id=session_id, trace_id=getattr(self, "_trace_id", None),
        )

        if extraction.action == "schedule":
            if not extraction.title:
                return "What should the meeting be called?", []
            if not extraction.scheduled_at:
                return "When should this meeting be scheduled?", []
            try:
                scheduled_at = datetime.fromisoformat(extraction.scheduled_at)
            except (TypeError, ValueError):
                return "I couldn't understand that date/time — could you rephrase it?", []
            team_id: int | None = None
            if extraction.team_name:
                team_id, team_warning = await self._resolve_team_id(extraction.team_name)
                if team_warning:
                    return team_warning, []
            project_id, project_warning = await self._resolve_project_id(extraction.project_name)
            if project_warning:
                return project_warning, []
            result = await run_tool(
                "schedule_meeting",
                {
                    "title": extraction.title, "scheduled_at": scheduled_at.isoformat(),
                    "duration_minutes": extraction.duration_minutes or 60,
                    "team_id": team_id, "project_id": project_id, "location": extraction.location,
                },
                ctx,
            )
            if result.ok and result.data and result.data.get("meeting_id"):
                self._record_step_result("meeting", result.data["meeting_id"])
            return result.message, result.actions

        # update
        if not extraction.meeting_reference:
            return "Which meeting would you like to update?", []
        meetings = await self._query_meetings(user=user, org_role=org_role)
        meeting_id = await self._resolve_reference_id(
            extraction.meeting_reference, meetings, name_fn=lambda m: m.title, label="meeting",
        )
        if isinstance(meeting_id, tuple):
            return meeting_id[1], []
        if not extraction.scheduled_at and not extraction.status:
            return "I understood you want to update the meeting but couldn't determine what to change.", []
        tool_params: dict = {"meeting_id": meeting_id}
        if extraction.scheduled_at:
            try:
                tool_params["scheduled_at"] = datetime.fromisoformat(extraction.scheduled_at).isoformat()
            except (TypeError, ValueError):
                return "I couldn't understand that date/time — could you rephrase it?", []
        if extraction.status:
            tool_params["status"] = extraction.status
        result = await run_tool("update_meeting", tool_params, ctx)
        return result.message, result.actions

    async def _handle_create_project(
        self, user: User, message: str, history: str = "",
        org_role: str = TEAM_MEMBER, session_id: int | None = None,
    ) -> tuple[str, list[ChatAction]]:
        user_prompt = f"Conversation history:\n{history}\n\nUser instruction: {message}" if history else message
        extraction = await self._llm.generate_structured(
            system_prompt=_CREATE_PROJECT_SYSTEM, user_prompt=user_prompt, schema=CreateProjectExtraction, temperature=0.1,
            capability="project_create_extraction",
        )
        if not extraction.name:
            return "What should the project be called?", []
        ctx = ToolContext(
            db=self._db, org_id=self._org_id, org_role=org_role, user=user, user_id=self._safe_user_id(user),
            session_id=session_id, trace_id=getattr(self, "_trace_id", None),
        )
        result = await run_tool("create_project", {"name": extraction.name, "description": extraction.description}, ctx)
        if result.ok and result.data and result.data.get("project_id"):
            self._record_step_result("project", result.data["project_id"])
        return result.message, result.actions

    async def _handle_create_team(
        self, user: User, message: str, history: str = "",
        org_role: str = TEAM_MEMBER, session_id: int | None = None,
    ) -> tuple[str, list[ChatAction]]:
        user_prompt = f"Conversation history:\n{history}\n\nUser instruction: {message}" if history else message
        extraction = await self._llm.generate_structured(
            system_prompt=_CREATE_TEAM_SYSTEM, user_prompt=user_prompt, schema=CreateTeamExtraction, temperature=0.1,
            capability="team_create_extraction",
        )
        if not extraction.name:
            return "What should the team be called?", []
        # No-silent-fallback (architecture item 6) — every team needs a real
        # manager; an unresolved/unmentioned name is surfaced, never defaulted
        # to the requesting user or left silently unset.
        if not extraction.team_manager_name:
            return "Who should manage this team?", []
        manager_id, manager_warning = await self._resolve_user_id(extraction.team_manager_name)
        if manager_warning:
            return manager_warning, []
        if manager_id is None:
            return f'I couldn\'t find a user named "{extraction.team_manager_name}" to manage this team.', []

        ctx = ToolContext(
            db=self._db, org_id=self._org_id, org_role=org_role, user=user, user_id=self._safe_user_id(user),
            session_id=session_id, trace_id=getattr(self, "_trace_id", None),
        )
        result = await run_tool(
            "create_team",
            {"name": extraction.name, "description": extraction.description, "team_manager_id": manager_id},
            ctx,
        )
        if result.ok and result.data and result.data.get("team_id"):
            self._record_step_result("team", result.data["team_id"])
        return result.message, result.actions

    async def _handle_manage_knowledge(
        self, user: User, message: str, history: str = "",
        org_role: str = TEAM_MEMBER, session_id: int | None = None,
    ) -> tuple[str, list[ChatAction]]:
        """architecture item 1 — Knowledge/RAG retrieval, write side: save a
        document/SOP into the knowledge base via the same typed-tool /
        policy / risk / verify / audit pipeline as every other domain
        write."""
        user_prompt = f"Conversation history:\n{history}\n\nUser instruction: {message}" if history else message
        extraction = await self._llm.generate_structured(
            system_prompt=_CREATE_KNOWLEDGE_DOCUMENT_SYSTEM, user_prompt=user_prompt,
            schema=CreateKnowledgeDocumentExtraction, temperature=0.1,
            capability="knowledge_document_extraction",
        )
        if not extraction.title or not extraction.content:
            return "What should the document be called, and what content should it contain?", []

        ctx = ToolContext(
            db=self._db, org_id=self._org_id, org_role=org_role, user=user, user_id=self._safe_user_id(user),
            session_id=session_id, trace_id=getattr(self, "_trace_id", None),
        )
        result = await run_tool(
            "create_knowledge_document",
            {
                "title": extraction.title, "content": extraction.content,
                "doc_type": extraction.doc_type or "general", "tags": extraction.tags or [],
            },
            ctx,
        )
        if result.ok and result.data and result.data.get("document_id"):
            self._record_step_result("knowledge_document", result.data["document_id"])
        return result.message, result.actions

    async def _handle_manage_project(
        self, user: User, message: str, history: str = "",
        org_role: str = TEAM_MEMBER, session_id: int | None = None,
    ) -> tuple[str, list[ChatAction]]:
        """architecture item 2 — Projects lifecycle. AUTO-tier: rename,
        change description, or change status (including "archive" ->
        status=cancelled)."""
        user_prompt = f"Conversation history:\n{history}\n\nUser instruction: {message}" if history else message
        extraction = await self._llm.generate_structured(
            system_prompt=_MANAGE_PROJECT_SYSTEM, user_prompt=user_prompt, schema=ManageProjectExtraction, temperature=0.1,
            capability="project_update_extraction",
        )
        if not extraction.project_reference:
            return "Which project would you like to update?", []

        projects = await self._get_projects()
        project_id = await self._resolve_reference_id(
            extraction.project_reference, projects, name_fn=lambda p: p.name, label="project",
        )
        if isinstance(project_id, tuple):
            return project_id[1], []
        if not extraction.name and not extraction.description and not extraction.status:
            return "I understood you want to update the project but couldn't determine what to change.", []

        ctx = ToolContext(
            db=self._db, org_id=self._org_id, org_role=org_role, user=user, user_id=self._safe_user_id(user),
            session_id=session_id, trace_id=getattr(self, "_trace_id", None),
        )
        result = await run_tool(
            "update_project",
            {"project_id": project_id, "name": extraction.name, "description": extraction.description, "status": extraction.status},
            ctx,
        )
        if result.ok and result.data and result.data.get("project_id"):
            self._record_step_result("project", result.data["project_id"])
        return result.message, result.actions

    async def _handle_manage_team(
        self, user: User, message: str, history: str = "",
        org_role: str = TEAM_MEMBER, session_id: int | None = None,
    ) -> tuple[str, list[ChatAction]]:
        """architecture item 2 — Teams lifecycle. "update" (rename/
        description) is AUTO-tier, same shape as manage_project above.
        "reassign_manager" is CONFIRM-tier (R3) — a leadership change goes
        through preview+confirm exactly like task reassignment does, via
        the same change-set / Transaction Coordinator infrastructure
        (see change_sets.build_change_set_for_entities and
        transaction._apply_reassign_team_manager)."""
        user_prompt = f"Conversation history:\n{history}\n\nUser instruction: {message}" if history else message
        extraction = await self._llm.generate_structured(
            system_prompt=_MANAGE_TEAM_SYSTEM, user_prompt=user_prompt, schema=ManageTeamExtraction, temperature=0.1,
            capability="team_update_extraction",
        )
        if not extraction.team_reference:
            return "Which team would you like to update?", []

        teams = await self._get_teams()
        team_id = await self._resolve_reference_id(
            extraction.team_reference, teams, name_fn=lambda t: t.name, label="team",
        )
        if isinstance(team_id, tuple):
            return team_id[1], []

        if extraction.action == "update":
            if not extraction.name and not extraction.description:
                return "I understood you want to update the team but couldn't determine what to change.", []
            ctx = ToolContext(
                db=self._db, org_id=self._org_id, org_role=org_role, user=user, user_id=self._safe_user_id(user),
                session_id=session_id, trace_id=getattr(self, "_trace_id", None),
            )
            result = await run_tool(
                "update_team", {"team_id": team_id, "name": extraction.name, "description": extraction.description}, ctx,
            )
            if result.ok and result.data and result.data.get("team_id"):
                self._record_step_result("team", result.data["team_id"])
            return result.message, result.actions

        # reassign_manager — CONFIRM-tier.
        refusal = check_write_authorized(org_role=org_role, tool_name="reassign_team_manager")
        if refusal is not None:
            return refusal, []
        if not extraction.new_manager_name:
            return "Who should be the new manager for this team?", []
        new_manager_id, manager_warning = await self._resolve_user_id(extraction.new_manager_name)
        if manager_warning:
            return manager_warning, []
        if new_manager_id is None:
            return f'I couldn\'t find a user named "{extraction.new_manager_name}".', []

        team = await self._team_repo.get_by_id(team_id)
        if team is None:
            return f"I couldn't find team #{team_id}.", []
        if session_id is None:
            return "Reassigning a team's manager needs an active chat session — please try again.", []

        change_set = await change_sets.build_change_set_for_entities(
            self._db,
            org_id=self._org_id, session_id=session_id, user_id=self._safe_user_id(user),
            tool_name="reassign_team_manager",
            params={"team_id": team.id, "new_manager_id": new_manager_id},
            affected=[("team", team.id, team.updated_at)],
            affected_summary=f'"{team.name}": new manager = {extraction.new_manager_name}',
        )
        await self._db.commit()
        return (
            f'This will reassign team "{team.name}" to be managed by {extraction.new_manager_name}. Please confirm to proceed.',
            [ChatAction(
                type="change_set_preview",
                label=f'Confirm manager reassignment for "{team.name}"',
                payload={"change_set_id": change_set.id, "affected_count": 1, "summary": f"new manager = {extraction.new_manager_name}"},
            )],
        )

    async def _handle_generate_report(
        self, user: User, message: str, history: str = "",
        org_role: str = TEAM_MEMBER, session_id: int | None = None,
    ) -> tuple[str, list[ChatAction]]:
        """architecture item 3 — Reporting domain, write side: generate a
        project status report via the same engine the HTTP Reports module
        uses (see tools/reporting_tools.py)."""
        user_prompt = f"Conversation history:\n{history}\n\nUser instruction: {message}" if history else message
        extraction = await self._llm.generate_structured(
            system_prompt=_GENERATE_REPORT_SYSTEM, user_prompt=user_prompt, schema=GenerateReportExtraction, temperature=0.1,
            capability="report_extraction",
        )
        if not extraction.project_reference:
            return "Which project would you like a report for?", []

        projects = await self._get_projects()
        project_id = await self._resolve_reference_id(
            extraction.project_reference, projects, name_fn=lambda p: p.name, label="project",
        )
        if isinstance(project_id, tuple):
            return project_id[1], []

        title = extraction.title.strip() if extraction.title and extraction.title.strip() else f"{extraction.project_reference} {extraction.report_type} report"
        ctx = ToolContext(
            db=self._db, org_id=self._org_id, org_role=org_role, user=user, user_id=self._safe_user_id(user),
            session_id=session_id, trace_id=getattr(self, "_trace_id", None),
        )
        result = await run_tool(
            "generate_project_report",
            {"project_id": project_id, "report_type": extraction.report_type, "title": title},
            ctx,
        )
        if result.ok and result.data and result.data.get("report_id"):
            self._record_step_result("report", result.data["report_id"])
        return result.message, result.actions

    async def _resolve_reference_id(self, ref: str, candidates: list, *, name_fn, label: str):
        """Shared by the Issue/Rock/KPI handlers above — a numeric reference
        resolves directly by ID; otherwise falls through to the same
        confidence-scored, no-silent-fallback resolver used for task/user/
        project/team references (architecture item 6). Returns the resolved
        int id, or a (None, message) tuple the caller returns verbatim on
        an unresolved/ambiguous reference."""
        ref = (ref or "").strip()
        if ref.isdigit():
            return int(ref)
        resolution = reference_resolver.resolve_by_name(candidates, ref, name_fn, lambda c: c.id)
        if resolution.status == ResolutionStatus.AMBIGUOUS:
            return None, resolution.clarification_message_for(label)
        if resolution.entity is None:
            return None, f'I couldn\'t find a {label} matching "{ref}".'
        return resolution.entity.id

    async def _handle_analyze_text(
        self, user: User, message: str, history: str = "",
        org_role: str = TEAM_MEMBER, session_id: int | None = None,
    ) -> tuple[str, list[ChatAction]]:
        # SECURITY FIX (same bug class as the original create_task gap — see
        # tools/registry.py's module docstring): this handler used to create
        # tasks directly via TaskRepository with NO org_role parameter and NO
        # authorization check at all, reachable by any role including CLIENT.
        refusal = check_write_authorized(org_role=org_role, tool_name="create_task")
        if refusal is not None:
            return refusal, []

        users_block = await self._build_users_block()
        system = (
            _ANALYZE_TEXT_SYSTEM
            .replace("{users_block}", users_block)
            .replace("{today}", await self._today_str())
        )
        user_prompt = (
            f"Conversation history:\n{history}\n\nUser instruction: {message}"
            if history else message
        )
        data = await self._llm.generate_json(
            system_prompt=system,
            user_prompt=user_prompt,
            temperature=0.1,
            capability="text_analysis_extraction",
        )

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

        # Create tasks via the Domain Tool Registry — schema validation +
        # centralized authorization + post-write verification + audit.
        tool_items = []
        resolution_warnings: list[str] = []
        for raw in raw_tasks:
            name = (raw.get("name") or "").strip()
            if not name:
                continue
            # No-silent-fallback (architecture item 6) — see the matching
            # comment in _handle_create_task.
            assignee_id, assignee_warning = await self._resolve_user_id(raw.get("assignee_name"), fallback=user.id)
            project_id, project_warning = await self._resolve_project_id(raw.get("project_name"))
            team_id, team_warning = await self._resolve_team_id(raw.get("team_name"))
            for warning in (assignee_warning, project_warning, team_warning):
                if warning:
                    resolution_warnings.append(f'"{name}": {warning}')
            tool_items.append({
                "name": name,
                "start_date": _parse_date(raw.get("start_date")),
                "due_date": _parse_date(raw.get("due_date")),
                "assignee_id": assignee_id,
                "project_id": project_id,
                "team_id": team_id,
            })

        if not tool_items:
            summary_line = f"\n\nSummary: {summary}" if summary else ""
            return f"I analyzed the text but couldn't find any clear action items.{summary_line}", []

        ctx = ToolContext(
            db=self._db, org_id=self._org_id, org_role=org_role, user=user, user_id=self._safe_user_id(user),
            session_id=session_id, trace_id=getattr(self, "_trace_id", None),
        )
        tool_result = await run_tool("create_task", {"tasks": tool_items}, ctx)
        if not tool_result.ok:
            return tool_result.message, []

        # Multi-goal intermediate-result propagation (architecture item 11) — see the matching comment in _handle_create_task.
        created_ids = (tool_result.data or {}).get("task_ids", [])
        if created_ids:
            self._record_step_result("task", created_ids[-1])

        for task_id in created_ids:
            task = await self._task_repo.get_by_id(task_id)
            if task is not None:
                await self._notify_assigned(task, assigned_by=user)

        summary_line = f"\n\n**Summary:** {summary}" if summary else ""
        names = [a.payload.get("task_name", "") for a in tool_result.actions]
        reply = (
            f"I extracted **{len(names)} task{'s' if len(names) > 1 else ''}** from the text:{summary_line}\n\n"
            + "\n".join(f"• {n}" for n in names)
        )
        if resolution_warnings:
            reply += "\n\n⚠️ " + " ".join(resolution_warnings)
        return reply, tool_result.actions

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
            # generate_structured() (architecture item 4/5) validates
            # sub_intent against the exact closed set _handle_db_query_impl
            # below can dispatch (DbQueryExtraction.sub_intent's Literal) —
            # an invented/typo'd sub_intent value is now repaired the same
            # way malformed JSON is, instead of silently reaching the
            # "unknown sub-intent" fallback at the bottom of this method
            # with no visibility into what the model actually said.
            extraction = await self._llm.generate_structured(
                system_prompt=_DB_QUERY_EXTRACT_SYSTEM,
                user_prompt=user_prompt,
                schema=DbQueryExtraction,
                temperature=0.0,
                capability="db_query_extraction",
            )
        except Exception:
            logger.warning("DB query extraction failed for query=%r — falling back to general", message, exc_info=True)
            return await self._handle_general(user, message, history)

        sub_intent: str = extraction.sub_intent
        user_name: str | None = extraction.user_name
        project_name: str | None = extraction.project_name
        team_name: str | None = extraction.team_name
        raw_status: str | None = extraction.status
        role: str | None = extraction.role
        days_ahead: int = extraction.days_ahead or 7
        target_self: bool = extraction.target_self
        scoreboard_period: str = extraction.period

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
            members = await self._user_repo.list_by_org_all(self._org_id)
            active = sum(1 for _, m in members if m.is_active)
            return (
                f"There are **{len(members)} user(s)** in this organization "
                f"({active} active, {len(members) - active} inactive)."
            ), []

        # ── user_list ─────────────────────────────────────────────────────────
        if sub_intent == "user_list":
            if not can_see_all:
                return "You don't have permission to list all users.", []
            members = await self._user_repo.list_by_org_all(self._org_id)
            if not members:
                return "There are no users in this organization.", []
            # Org-scoped role (membership.role), not the legacy/global
            # User.role column — the two can diverge per-organization.
            lines = [
                f"• **{u.full_name}** ({u.email}) — {m.role.replace('_', ' ')} "
                f"— {'active' if m.is_active else 'inactive'}"
                for u, m in members[:50]
            ]
            return f"**Users ({len(members)} total):**\n" + "\n".join(lines), []

        # ── user_by_role ──────────────────────────────────────────────────────
        if sub_intent == "user_by_role":
            if not can_see_all:
                return "You don't have permission to view user roles.", []
            if not role:
                return "Which role are you asking about? (admin, team_manager, or team_member)", []
            users = await self._user_repo.list_by_org_and_roles(self._org_id, [role])
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
            today = await self._org_today()
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
            today = await self._org_today()
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
            projects = await self._query_projects(user=user, org_role=org_role)
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
            projects = await self._query_projects(user=user, org_role=org_role)
            if not projects:
                return "There are no projects in the system.", []
            lines = [f"• [{p.id}] **{p.name}** — {p.status}" for p in projects[:30]]
            return f"**Projects ({len(projects)} total):**\n" + "\n".join(lines), []

        # ── project_by_status ─────────────────────────────────────────────────
        if sub_intent == "project_by_status":
            if not can_see_all:
                return "You don't have permission to view project statistics.", []
            projects = await self._query_projects(user=user, org_role=org_role)
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
            teams = await self._query_teams(user=user, org_role=org_role)
            return f"There are **{len(teams)} team(s)** in the system.", []

        # ── team_list ─────────────────────────────────────────────────────────
        if sub_intent == "team_list":
            if not can_see_all:
                return "You don't have permission to list all teams.", []
            teams = await self._query_teams(user=user, org_role=org_role)
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
            users = await self._user_repo.list_by_org_all(self._org_id)
            projects = await self._query_projects(user=user, org_role=org_role)
            teams = await self._query_teams(user=user, org_role=org_role)
            today = await self._org_today()
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
            rocks = await self._query_rocks(user=user, org_role=org_role)
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
            rocks = await self._query_rocks(team_id=team.id, user=user, org_role=org_role)
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
            issues = await self._query_issues(user=user, org_role=org_role)
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
            issues = await self._query_issues(open_only=True, user=user, org_role=org_role)
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
            team_id = None
            if team_name:
                # No-silent-fallback (architecture item 6): a team name that
                # was given but doesn't resolve must not silently become
                # "show every team's KPIs" — that's a different answer than
                # what was asked, not a safe default.
                team_id, team_warning = await self._resolve_team_id(team_name)
                if team_warning:
                    return team_warning, []
            kpis = await self._query_kpis(team_id, user=user, org_role=org_role)
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
            meetings = await self._query_meetings(upcoming_only=upcoming_only, user=user, org_role=org_role)
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

        # ── my_scoreboard ────────────────────────────────────────────────────
        if sub_intent == "my_scoreboard":
            ctx = self._read_tool_ctx(user, org_role)
            result = await run_tool("get_my_scoreboard", {"period": scoreboard_period}, ctx)
            if not result.ok:
                return result.message, []
            data = result.data["scoreboard"]
            current = data.current
            if not current.has_data:
                return f"No completed or assigned tasks found for {data.period.replace('_', ' ')} — nothing to score yet.", []
            change_line = ""
            if data.change_from_previous is not None:
                sign = "+" if data.change_from_previous >= 0 else ""
                change_line = f" ({sign}{data.change_from_previous} vs. previous period)"
            return (
                f"**Your scoreboard — {data.period.replace('_', ' ')}:**\n"
                f"Score: {current.rounded_score}/100{change_line} — {current.performance_level}\n"
                f"• Completed: {current.total_completed}/{current.total_assigned}\n"
                f"• On-time rate: {round(current.on_time_rate * 100)}%\n"
                f"• Overdue: {current.overdue}"
            ), []

        # ── team_scoreboard (architecture item 3 — manager-tier scoreboard) ──
        if sub_intent == "team_scoreboard":
            if not team_name:
                return "Which team's scoreboard would you like to see?", []
            team = await self._resolve_team(team_name)
            if not team:
                return f"I couldn't find a team named **{team_name}**.", []
            ctx = self._read_tool_ctx(user, org_role)
            result = await run_tool("get_team_scoreboard", {"team_id": team.id, "period": scoreboard_period}, ctx)
            if not result.ok:
                return result.message, []
            data = result.data["scoreboard"]
            current = data.current
            if not current.has_data:
                return f"No completed or assigned tasks found for team **{team.name}** in {data.period.replace('_', ' ')} — nothing to score yet.", []
            member_lines = [f"• #{m.rank} {m.full_name}: {m.result.rounded_score}/100" for m in data.members[:10]]
            return (
                f"**Team \"{result.data['team_name']}\" scoreboard — {data.period.replace('_', ' ')}:**\n"
                f"Score: {current.rounded_score}/100 — {current.performance_level}\n"
                f"• Completed: {current.total_completed}/{current.total_assigned}\n"
                f"• On-time rate: {round(current.on_time_rate * 100)}%\n\n"
                "**Members:**\n" + "\n".join(member_lines)
            ), []

        # ── org_scoreboard (architecture item 3 — manager-tier scoreboard) ──
        if sub_intent == "org_scoreboard":
            ctx = self._read_tool_ctx(user, org_role)
            result = await run_tool("get_org_scoreboard", {"period": scoreboard_period}, ctx)
            if not result.ok:
                return result.message, []
            rows = result.data["rows"]
            if not rows:
                return f"No scored employees found for {result.data['period'].replace('_', ' ')}.", []
            lines = [f"• #{r.rank} {r.full_name} ({r.team_name or 'no team'}): {r.result.rounded_score}/100" for r in rows[:15]]
            return f"**Organization leaderboard — {result.data['period'].replace('_', ' ')}:**\n" + "\n".join(lines), []

        # ── search_everything (architecture item 10 — hybrid keyword retrieval) ──
        if sub_intent == "search_everything":
            ctx = self._read_tool_ctx(user, org_role)
            result = await run_tool("search_everything", {"query": message}, ctx)
            if not result.ok:
                return result.message, []
            items = result.data["items"]
            if not items:
                return f'I couldn\'t find anything matching "{message}".', []
            lines = [f"• [{item['entity_type']}] **{item['name']}** (#{item['id']})" for item in items]
            return f"**Found {len(items)} result(s):**\n" + "\n".join(lines), []

        # ── search_knowledge (architecture item 1 — Knowledge/RAG retrieval) ──
        if sub_intent == "search_knowledge":
            ctx = self._read_tool_ctx(user, org_role)
            result = await run_tool("search_knowledge", {"query": message}, ctx)
            if not result.ok:
                return result.message, []
            items = result.data["items"]
            if not items:
                return f'I couldn\'t find anything in the knowledge base matching "{message}".', []
            lines = [f"• **{item['title']}** ({item['doc_type']}) — {item['snippet']}" for item in items]
            return f"**Found {len(items)} document(s):**\n" + "\n".join(lines), []

        # Unknown sub-intent — fall back to general
        logger.info("DB query sub_intent %r unrecognized — falling back to general", sub_intent)
        return await self._handle_general(user, message, history)

    # ─── Rocks / Issues (org-scoped, direct queries — no dedicated repository) ──

    def _read_tool_ctx(self, user: User, org_role: str) -> ToolContext:
        return ToolContext(
            db=self._db, org_id=self._org_id, org_role=org_role, user=user, user_id=self._safe_user_id(user),
            session_id=None, trace_id=getattr(self, "_trace_id", None),
        )

    async def _query_rocks(
        self, team_id: int | None = None, *, user: User, org_role: str = TEAM_MEMBER,
    ) -> list[Rock]:
        # Delegates to the Domain Tool Registry's search_rocks (schema
        # validation + centralized read-authorization + audit); the query
        # itself is unchanged from before this pass, only moved into
        # read_tools.py so it's a named, typed, audited tool.
        ctx = self._read_tool_ctx(user, org_role)
        result = await run_tool("search_rocks", {"team_id": team_id}, ctx)
        if not result.ok:
            return []
        return (result.data or {}).get("items", [])

    async def _query_issues(
        self, *, open_only: bool = False, user: User, org_role: str = TEAM_MEMBER,
    ) -> list[Issue]:
        ctx = self._read_tool_ctx(user, org_role)
        result = await run_tool("search_issues", {"open_only": open_only}, ctx)
        if not result.ok:
            return []
        return (result.data or {}).get("items", [])

    async def _team_name_lookup(self) -> dict[int, str]:
        teams = await self._get_teams()
        return {t.id: t.name for t in teams}

    async def _query_kpis(
        self, team_id: int | None = None, *, user: User, org_role: str = TEAM_MEMBER,
    ) -> list["KPI"]:
        ctx = self._read_tool_ctx(user, org_role)
        result = await run_tool("search_kpis", {"team_id": team_id}, ctx)
        if not result.ok:
            return []
        return (result.data or {}).get("items", [])

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

    async def _query_meetings(
        self, *, upcoming_only: bool = False, user: User, org_role: str = TEAM_MEMBER,
    ) -> list["Meeting"]:
        ctx = self._read_tool_ctx(user, org_role)
        result = await run_tool("search_meetings", {"upcoming_only": upcoming_only}, ctx)
        if not result.ok:
            return []
        return (result.data or {}).get("items", [])

    async def _query_client_requests(self, *, user: User, org_role: str) -> list["TaskRequest"]:
        ctx = self._read_tool_ctx(user, org_role)
        result = await run_tool("search_client_requests", {}, ctx)
        if not result.ok:
            return []
        return (result.data or {}).get("items", [])

    async def _query_projects(self, *, user: User, org_role: str) -> list:
        # Security gap #6 (strict acceptance audit) — project_count/
        # project_list/project_by_status used to call self._project_repo
        # directly, bypassing the Tool Registry entirely (no schema
        # validation, no read-authorization check, no audit row).
        ctx = self._read_tool_ctx(user, org_role)
        result = await run_tool("search_projects", {}, ctx)
        if not result.ok:
            return []
        return (result.data or {}).get("items", [])

    async def _query_teams(self, *, user: User, org_role: str) -> list:
        # Same fix as _query_projects, for team_count/team_list/
        # team_members/team_workload.
        ctx = self._read_tool_ctx(user, org_role)
        result = await run_tool("search_teams", {}, ctx)
        if not result.ok:
            return []
        return (result.data or {}).get("items", [])

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
            capability="general_response",
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

    async def _store_result_set(self, session_id: int | None, entity_type: str, ids: list[int]) -> None:
        """Records the ordered id list from the most recent list-type reply
        (e.g. "show me overdue tasks") so a later positional follow-up
        ("mark the second one done") can resolve deterministically against
        exactly what the user was just shown. Best-effort — a failure here
        must not break the chat reply that already succeeded."""
        if session_id is None or not ids:
            return
        try:
            await self._db.execute(
                update(ChatSession)
                .where(ChatSession.id == session_id)
                .values(last_result_set_json={"entity_type": entity_type, "ids": ids[:50]})
            )
            await self._db.commit()
        except Exception:
            logger.warning("Failed to store result-set memory for session=%s", session_id, exc_info=True)

    async def _resolve_positional_task_id(self, session_id: int | None, ordinal_position: int | None) -> int | None:
        """Returns the task id at the ordinal position the LLM already
        classified (architecture item 8, language-agnostic refactor):
        `ordinal_position` is a canonical 1-based position (1=first,
        2=second, ...) or -1 for "the last one" — produced by the
        extraction LLM from whatever language/script the user wrote
        ("the second one", "দ্বিতীয়টা", "dwitiyo", any other language's
        equivalent all arrive here as the identical integer 2). This
        function does no language understanding at all — it only indexes
        into the session's stored result set. Returns None if there's no
        position to resolve, no stored result set, or the position is out
        of range (never silently clamped to the nearest valid index — an
        out-of-range ordinal is a failed resolution, not a guess)."""
        if session_id is None or ordinal_position is None:
            return None
        session = await self._chat_repo.get_session(session_id)
        if session is None or not session.last_result_set_json:
            return None
        result_set = session.last_result_set_json
        if result_set.get("entity_type") != "task":
            return None
        ids: list[int] = result_set.get("ids") or []
        if not ids:
            return None
        if ordinal_position == -1:
            return ids[-1]
        index = ordinal_position - 1  # canonical ordinal_position is 1-based
        if 0 <= index < len(ids):
            return ids[index]
        return None

    def _resolve_deictic_entity_id(self, is_deictic: bool, entity_type: str = "task") -> int | None:
        """Architecture item 9 — validated UI/page context, language-agnostic
        refactor: `is_deictic` is the LLM's already-made classification of
        whether the user meant "this"/"it"/"the current one" in whatever
        language they wrote — this function does no word-matching of any
        kind, it only decides WHERE to resolve a deictic reference to once
        the LLM has said the message contains one.

        Resolves directly to the record the frontend told us the user is
        looking at — deterministically, never guessed. An explicitly
        named/ID'd reference always wins; this is consulted only when the
        LLM classified the reference as deictic, and only when the frontend
        actually told us the page context (page_context is optional — most
        chat turns, e.g. from the floating widget on a list page, won't
        have one, and that's fine, just no deictic shortcut is available).

        Falls back to this turn's step-result propagation (architecture
        item 11 — multi-goal intermediate-result propagation, generalized
        beyond tasks — see _record_step_result()) when there's no page
        context to consult: "create an issue for the outage and then
        resolve it" has no page open at all — "it" can only be resolved
        against what THIS turn's earlier step just touched, for whichever
        `entity_type` the CURRENT step cares about (an issue-status update
        resolving "it" must never accidentally pick up a task id from an
        earlier step in the same multi-goal message).
        """
        if not is_deictic:
            return None
        if self._page_context is not None and self._page_context.page_type == entity_type and self._page_context.entity_id is not None:
            return self._page_context.entity_id
        return self._step_results.get(entity_type)

    async def _resolve_task_reference(
        self, *, session_id: int | None, reference_type: str,
        task_reference: str | None, ordinal_position: int | None,
        candidate_tasks: list,
    ) -> tuple[Task | None, str | None]:
        """Canonical, language-agnostic task reference resolution shared by
        _handle_update_task and _handle_delete_task (architecture items
        7-9, language-agnostic refactor). `reference_type` is the LLM's own
        classification (see _UPDATE_TASK_SYSTEM/_DELETE_TASK_SYSTEM and
        intent_schemas.py's canonical-reference-classification docstring)
        — this function only branches on that classification; it never
        pattern-matches reference text against language-specific ordinal
        or pronoun words. Returns (task_or_None, ambiguous_clarification_
        message_or_None) — a non-None clarification means the caller should
        surface it directly rather than treating the task as unresolved.
        """
        if reference_type == "ordinal":
            task_id = await self._resolve_positional_task_id(session_id, ordinal_position)
            return (await self._task_repo.get_by_id(task_id) if task_id is not None else None), None

        if reference_type == "deictic":
            task_id = self._resolve_deictic_entity_id(True, "task")
            return (await self._task_repo.get_by_id(task_id) if task_id is not None else None), None

        # "explicit" (and the default/fallback for any unrecognized value —
        # never silently resolves to something the LLM didn't actually say).
        ref = (task_reference or "").strip()
        if not ref:
            return None, None
        if ref.isdigit():
            return await self._task_repo.get_by_id(int(ref)), None
        resolution = await reference_resolver.resolve_task_reference(self._db, candidate_tasks, ref)
        if resolution.status == ResolutionStatus.AMBIGUOUS:
            return None, resolution.clarification_message
        return resolution.entity, None

    async def _resolve_user_id(
        self, name: str | None, fallback: int | None = None
    ) -> tuple[int | None, str | None]:
        """Returns (resolved_user_id, unresolved_warning).

        SECURITY/CORRECTNESS FIX (architecture item 6 — "safe deterministic
        entity/reference resolution with no silent fallback"): this used to
        do a naive first-substring-match scan and, when `name` was given
        but matched nobody (or ambiguously matched several people whose
        names/emails both happened to contain the fragment), silently
        returned `fallback` — usually the requesting user's own id. That
        means "assign this to Sarah" for a nonexistent/misspelled "Sarah"
        would silently assign the task to whoever typed the message, with
        no indication anything went wrong. `fallback` is now used ONLY when
        `name` itself is empty (i.e. no assignee was mentioned at all —
        a legitimate default, not a failed resolution). Any name that
        WAS given must resolve through reference_resolver.resolve_by_name()
        (same confidence-scored, ambiguity-aware resolver already used for
        task references) or the caller gets an explicit warning back
        instead of a silently-wrong id.
        """
        if not name:
            return fallback, None
        users = await self._get_users()
        resolution = reference_resolver.resolve_by_name(users, name, lambda u: u.full_name, lambda u: u.id)
        if resolution.status == ResolutionStatus.RESOLVED:
            return resolution.entity.id, None
        if resolution.status == ResolutionStatus.AMBIGUOUS:
            return None, f'"{name}" matches more than one person, so I left it unassigned — please specify a full name or email.'
        return None, f'I couldn\'t find a user matching "{name}", so I left it unassigned.'

    async def _resolve_user_by_name(self, name: str) -> User | None:
        users = await self._get_users()
        resolution = reference_resolver.resolve_by_name(users, name, lambda u: u.full_name, lambda u: u.id)
        return resolution.entity if resolution.status == ResolutionStatus.RESOLVED else None

    async def _resolve_project_id(self, name: str | None) -> tuple[int | None, str | None]:
        if not name:
            return None, None
        projects = await self._get_projects()
        resolution = reference_resolver.resolve_by_name(projects, name, lambda p: p.name, lambda p: p.id)
        if resolution.status == ResolutionStatus.RESOLVED:
            return resolution.entity.id, None
        if resolution.status == ResolutionStatus.AMBIGUOUS:
            return None, f'"{name}" matches more than one project, so I left it unassigned — please specify the exact project name.'
        return None, f'I couldn\'t find a project matching "{name}", so I left it unassigned.'

    async def _resolve_project(self, name: str):
        projects = await self._get_projects()
        resolution = reference_resolver.resolve_by_name(projects, name, lambda p: p.name, lambda p: p.id)
        return resolution.entity if resolution.status == ResolutionStatus.RESOLVED else None

    async def _resolve_team_id(self, name: str | None) -> tuple[int | None, str | None]:
        if not name:
            return None, None
        teams = await self._get_teams()
        resolution = reference_resolver.resolve_by_name(teams, name, lambda t: t.name, lambda t: t.id)
        if resolution.status == ResolutionStatus.RESOLVED:
            return resolution.entity.id, None
        if resolution.status == ResolutionStatus.AMBIGUOUS:
            return None, f'"{name}" matches more than one team, so I left it unassigned — please specify the exact team name.'
        return None, f'I couldn\'t find a team matching "{name}", so I left it unassigned.'

    async def _resolve_team(self, name: str):
        teams = await self._get_teams()
        resolution = reference_resolver.resolve_by_name(teams, name, lambda t: t.name, lambda t: t.id)
        return resolution.entity if resolution.status == ResolutionStatus.RESOLVED else None

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
