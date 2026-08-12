"""Domain Tool Registry + controlled execution pipeline (architecture
Section 4.5 / 25 / 26).

Before this module existed, "low risk" (R0-R2) write actions — task
creation and single-field task edits — were executed directly inside
ChatService's handlers: raw dict from the LLM -> straight into a
TaskCreate/TaskUpdate call, with NO schema validation, NO centralized
policy check (this is exactly how a CLIENT user could create/update real
tasks through chat — see the audit note below), NO audit row, and NO
post-write verification. Only the R3+ (confirm-tier) actions went through
the more rigorous change-set + Transaction Coordinator path.

`run_tool()` is now the single entry point for every AUTO-tier
(auto-executing) write tool:

    schema validation -> policy check -> risk gate -> execute -> verify -> audit

CONFIRM-tier tools (reassign/delete/bulk-update/convert) still build a
change set via app.services.copilot.change_sets and execute later through
app.services.copilot.transaction.execute_confirmed_change_set — that path
was already correct (and is one of the four already-accepted fixes from
this session; it is not touched here). Those handlers now call
`check_write_authorized()` from this module at their own entry point so
the SAME centralized policy decision applies to every write tool, not just
the AUTO-tier ones — closing the authorization gap structurally rather
than patching each handler with its own ad hoc role check (which is
exactly how the gap happened in the first place).

SECURITY NOTE — bug found and fixed while building this registry:
`ChatService._handle_create_task()` had NO org_role parameter and NO
permission check at all; `_route()`'s entry gate only blocked
`TEAM_MEMBER`, not `CLIENT`. A client user could say "create a task
called X" and it would actually create one, directly contradicting
policy.py's own documented rule ("Clients have no write tools available
through chat at all today"). `_handle_update_task()` had the same gap for
CLIENT on their own assigned tasks. Both are fixed by routing through this
registry / `check_write_authorized()`, which is now the single place that
decision is made.
"""

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.org_roles import CLIENT
from app.models.user import User
from app.schemas.chat import ChatAction
from app.services.copilot import audit, policy, risk

logger = logging.getLogger("copilot.tools")


@dataclass
class ToolContext:
    db: AsyncSession
    org_id: object  # uuid.UUID
    org_role: str
    user: User
    # A required, explicitly-passed plain int — deliberately NOT derived
    # from `user.id` internally (e.g. via __post_init__). A soft tool
    # failure (see run_tool()'s partial-failure fix, item 16) calls
    # db.rollback(), which expires every ORM attribute on every object
    # still attached to the session — including `user`, even if `user` had
    # nothing to do with that particular failed call. A LATER ToolContext
    # built from the same (now-expired) `user` object — e.g. the second
    # step of the Structured Planner running several tool calls
    # sequentially against one shared session — would hit this the moment
    # anything tried to read `user.id` again, including inside this
    # dataclass's own construction. Callers must capture the id once, early
    # (see ChatService._safe_user_id()), and pass that cached int in on
    # every subsequent construction instead of ever re-reading `user.id`.
    user_id: int
    session_id: int | None
    trace_id: str | None = None


@dataclass
class ToolResult:
    ok: bool
    message: str
    actions: list[ChatAction] = field(default_factory=list)
    data: dict | None = None


ToolHandler = Callable[[ToolContext, BaseModel], Awaitable[ToolResult]]


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: type[BaseModel]
    # Only AUTO-tier tools carry a handler — CONFIRM-tier tools are applied
    # later by app.services.copilot.transaction, keyed by the same tool
    # name (see risk.RISK_TABLE / transaction.execute_confirmed_change_set).
    handler: ToolHandler | None = None
    # Coarse RBAC override layered on top of policy.check_tool_policy's
    # CLIENT-always-denied rule — e.g. task mutation tools additionally
    # exclude TEAM_MEMBER (existing, deliberate product rule: "contact your
    # team manager or admin to make task changes"). None = policy.py's
    # default (deny CLIENT, allow everyone else) is the whole rule.
    allowed_roles: frozenset[str] | None = None
    # "write" (default) tools go through the full write pipeline below
    # (policy.check_tool_policy denies CLIENT unconditionally — correct for
    # every write action, since "clients have no write tools available
    # through chat" is an absolute product rule). "read" tools are
    # side-effect-free (search/list/get) and use the lighter read pipeline:
    # schema validate -> read-authorization (client_blocked flag, matching
    # the pre-existing policy.CLIENT_BLOCKED_SUB_INTENTS rule) -> execute ->
    # audit. No risk gate, no post-write verification, no ABAC needed beyond
    # what the query itself already scopes (e.g. client_request search
    # scopes to the caller's own submissions inside the handler).
    kind: str = "write"
    # Read tools only: mirrors policy.CLIENT_BLOCKED_SUB_INTENTS — True for
    # org-wide/internal-staff data (rocks, issues, KPIs, meetings), False for
    # data a client may legitimately ask about themselves (their own
    # submitted task requests).
    client_blocked: bool = False


TOOL_REGISTRY: dict[str, ToolSpec] = {}


def register_tool(spec: ToolSpec) -> None:
    TOOL_REGISTRY[spec.name] = spec


def check_write_authorized(*, org_role: str, tool_name: str) -> str | None:
    """The single centralized authorization decision for a write tool —
    used both by run_tool() (AUTO tier) and directly by the CONFIRM-tier
    handlers in chat_service.py, so a write action can never reach a
    repository without passing through exactly this check.

    Returns None if allowed, or a user-facing refusal message if not.
    """
    spec = TOOL_REGISTRY.get(tool_name)

    decision = policy.check_tool_policy(org_role=org_role, tool_name=tool_name)
    if decision == policy.DENY:
        return (
            "You don't have permission to do that through the assistant. "
            "Please contact your team manager or admin."
        )

    if spec is not None and spec.allowed_roles is not None and org_role not in spec.allowed_roles:
        return (
            "You don't have permission to create, update, or delete tasks through the assistant. "
            "Please contact your team manager or admin to make task changes."
        )

    return None


def check_read_authorized(*, org_role: str, tool_name: str) -> str | None:
    """The read-side counterpart to check_write_authorized() — reuses the
    exact same CLIENT-visibility rule that already governed db_query's
    sub-intent dispatch (policy.CLIENT_BLOCKED_SUB_INTENTS), now expressed
    per-tool via ToolSpec.client_blocked so every entry point (the legacy
    sub-intent dispatch AND the tool registry) shares one source of truth
    instead of two independently-maintained lists drifting apart."""
    if org_role != CLIENT:
        return None
    spec = TOOL_REGISTRY.get(tool_name)
    if spec is not None and spec.client_blocked:
        return policy.CLIENT_REFUSAL_MESSAGE
    return None


async def run_tool(tool_name: str, raw_params: dict, ctx: ToolContext) -> ToolResult:
    spec = TOOL_REGISTRY.get(tool_name)
    if spec is None:
        logger.warning("run_tool: unknown tool_name=%r requested", tool_name)
        return ToolResult(False, "I don't have a way to do that yet.")

    # ctx.user_id is a plain int captured once at ToolContext construction
    # (see its docstring) — unlike ctx.user.id, it's always safe to read
    # here, even after a rollback elsewhere in this function has expired
    # every ORM object attached to the session.
    user_id = ctx.user_id

    # 1. Strict schema validation — reject anything that doesn't match the
    #    tool's declared contract instead of passing a raw dict through.
    try:
        params = spec.input_schema(**raw_params)
    except ValidationError as exc:
        logger.info("run_tool: schema validation failed for tool=%s errors=%s", tool_name, exc.errors())
        return ToolResult(False, "I couldn't apply that — some required details were missing or invalid.")

    if spec.kind == "read":
        refusal = check_read_authorized(org_role=ctx.org_role, tool_name=tool_name)
        if refusal is not None:
            return ToolResult(False, refusal)
        if spec.handler is None:
            logger.error("run_tool: read tool=%s has no handler registered", tool_name)
            return ToolResult(False, "Something is misconfigured for that lookup.")
        try:
            result = await spec.handler(ctx, params)
        except Exception as exc:
            logger.exception("run_tool: read handler failed for tool=%s", tool_name)
            await audit.log_tool_execution(
                ctx.db, org_id=ctx.org_id, session_id=ctx.session_id, user_id=user_id,
                tool_name=tool_name, risk_level="R0", policy_decision="allow",
                params=raw_params, result_summary=f"Read failed: {exc}", success=False,
                trace_id=ctx.trace_id,
            )
            return ToolResult(False, "Something went wrong looking that up.")
        await audit.log_tool_execution(
            ctx.db, org_id=ctx.org_id, session_id=ctx.session_id, user_id=user_id,
            tool_name=tool_name, risk_level="R0", policy_decision="allow",
            params=raw_params, result_summary=(result.message or "ok")[:500], success=result.ok,
            trace_id=ctx.trace_id,
        )
        return result

    # 2. Centralized authorization — the ONLY place this decision is made.
    refusal = check_write_authorized(org_role=ctx.org_role, tool_name=tool_name)
    if refusal is not None:
        return ToolResult(False, refusal)

    # 3. Risk gate.
    action = risk.risk_action(tool_name)
    if action == risk.BLOCK:
        return ToolResult(False, "That action isn't available through the assistant, for safety.")
    if action == risk.CONFIRM:
        # AUTO-tier-only dispatcher — CONFIRM-tier tools don't have a
        # `handler` registered; the caller is expected to build a change
        # set itself (see the module docstring). Reaching this branch for
        # a CONFIRM tool is a registration bug, not a runtime user error.
        logger.error("run_tool: tool=%s is CONFIRM-tier but was dispatched via the AUTO-only run_tool()", tool_name)
        return ToolResult(False, "That action needs confirmation and can't be completed this way.")

    if spec.handler is None:
        logger.error("run_tool: tool=%s has risk_action=AUTO but no handler registered", tool_name)
        return ToolResult(False, "Something is misconfigured for that action. Nothing was changed.")

    # 4. Idempotency (architecture item 13): a deterministic hash of
    #    (session, tool, params) — a second identical call within the dedup
    #    window (e.g. a client-side retry after a dropped response) returns
    #    the first call's recorded outcome instead of executing again and
    #    creating a duplicate. Only scoped when a session_id exists (nothing
    #    to scope the dedup key to otherwise); computed here, centrally, so
    #    no individual tool handler has to implement its own replay guard.
    idempotency_key = _compute_idempotency_key(ctx.session_id, tool_name, params)
    if idempotency_key is not None:
        prior = await audit.find_recent_success(
            ctx.db, org_id=ctx.org_id, tool_name=tool_name, idempotency_key=idempotency_key,
        )
        if prior is not None:
            logger.info(
                "run_tool: idempotency dedup hit for tool=%s key=%s (original at %s)",
                tool_name, idempotency_key, prior.created_at,
            )
            # Actions (e.g. task_created pills) aren't reconstructed from
            # the audit row — only the human-readable outcome is replayed —
            # so the reply text still confirms what happened even though no
            # new action pill is attached to this (repeated) turn.
            return ToolResult(True, prior.result_summary)

    # 5. Execute (AUTO tier only) -> verify (inside the handler) -> audit.
    try:
        result = await spec.handler(ctx, params)
    except Exception as exc:
        await ctx.db.rollback()
        logger.exception("run_tool: handler failed for tool=%s", tool_name)
        await audit.log_tool_execution(
            ctx.db, org_id=ctx.org_id, session_id=ctx.session_id, user_id=user_id,
            tool_name=tool_name, risk_level=risk.risk_level(tool_name), policy_decision="allow",
            params=raw_params, result_summary=f"Execution failed: {exc}", success=False,
            trace_id=ctx.trace_id, idempotency_key=idempotency_key,
        )
        return ToolResult(False, "Something went wrong applying that. Nothing was changed.")

    if not result.ok:
        # PARTIAL-FAILURE FIX (architecture item 16): a handler can fail
        # "softly" — returning ToolResult(False, ...) after its own
        # post-write verification found a problem, without raising — and
        # AUTO-tier handlers only flush(), never commit(), specifically so
        # this rollback can still undo the mutation. Without this, the
        # audit call below (which always commits) would have silently
        # persisted whatever was flushed even though the user is being told
        # the action failed and nothing changed.
        await ctx.db.rollback()

    await audit.log_tool_execution(
        ctx.db, org_id=ctx.org_id, session_id=ctx.session_id, user_id=user_id,
        tool_name=tool_name, risk_level=risk.risk_level(tool_name), policy_decision="allow",
        params=raw_params, result_summary=result.message, success=result.ok,
        trace_id=ctx.trace_id, idempotency_key=idempotency_key,
    )
    return result


def _compute_idempotency_key(session_id: int | None, tool_name: str, params: BaseModel) -> str | None:
    if session_id is None:
        return None
    payload = f"{session_id}:{tool_name}:{params.model_dump_json()}"
    return hashlib.sha256(payload.encode()).hexdigest()
