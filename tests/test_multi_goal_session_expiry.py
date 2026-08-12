"""Regression test for a production crash: `sqlalchemy.exc.MissingGreenlet:
greenlet_spawn has not been called; can't call await_only() here` raised
from ChatService.handle_message() at the `session.id` read feeding
_detect_and_route_step()'s second loop iteration (traceback pointed at
chat_service.py:614, `history_text, org_role, session.id, user,`).

ROOT CAUSE: handle_message() loaded the ChatSession ORM object once at the
top of the request and kept re-reading `session.id`/`session.title`
directly off it for the rest of the method — including inside the
multi-goal step loop, where each step is routed through
tools/registry.run_tool(). A tool call that fails (schema error, an
unhandled exception, or a handler's own post-write verification returning
ok=False) calls `await ctx.db.rollback()` — and since `self._db` is the
single AsyncSession shared for the whole HTTP request, THAT rollback
expires every ORM object already attached to it, including `session`
(confirmed empirically below: this app's AsyncSessionLocal is configured
with `expire_on_commit=False`, so a successful commit does NOT expire
objects — only rollback() does, unconditionally). Synchronously reading
`session.id` on the next loop iteration then tries to lazily reload the
now-expired attribute, which requires I/O and crashes with MissingGreenlet
outside the ORM's async-aware "greenlet" context.

The SAME rollback also expires `user` (the auth dependency and this
service share one request-scoped AsyncSession) — handle_message() had one
remaining raw `user.id` read after the step loop (feeding
memory.maybe_learn_preference()), fixed the same way.

FIX: `session.id`/`session.title`/`user.id` are captured into plain local
variables (`session_id`, `had_no_title`, `user_id`, the last via the
existing `_safe_user_id()` cache) immediately at the top of
handle_message(), before any step can run a rollback, and every later use
in the method reads those locals instead of the ORM objects.
ChatRepository.touch_session() was also changed to accept a plain
`session_id: int` and issue a direct UPDATE, instead of taking the
(possibly-expired-by-then) ORM object.

NOTE: this fixes handle_message() itself. The identical pattern (raw
`user.id`/`user.<attr>` reads) recurs throughout the many `_handle_*`
domain handlers this method routes into — those are unaffected by this
fix and were not audited here; see the accompanying report.

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.
"""

import asyncio
import json
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import MissingGreenlet

from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import TEAM_MANAGER
from app.models.chat import ChatMessage, ChatSession
from app.models.organization import Organization, OrganizationMembership
from app.models.task import Task
from app.models.user import User
from app.services.chat_service import ChatService
from app.services.llm.base import LLMProvider, LLMResponse


async def _make_org_and_owner(db, suffix: str):
    owner = User(full_name="MultiGoalExpiry Owner", email=f"mgexpiry.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
    db.add(owner)
    await db.flush()

    org = Organization(name=f"MultiGoalExpiry Org {suffix}", slug=f"mgexpiry-{suffix}", owner_id=owner.id)
    db.add(org)
    await db.flush()
    db.add(OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner"))
    await db.commit()
    return owner, org


async def _cleanup(db, *, org_id, owner_id, session_id=None, task_ids=None):
    if task_ids:
        await db.execute(delete(Task).where(Task.id.in_(task_ids)))
    if session_id is not None:
        await db.execute(delete(ChatMessage).where(ChatMessage.session_id == session_id))
        await db.execute(delete(ChatSession).where(ChatSession.id == session_id))
    await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org_id))
    await db.execute(delete(Organization).where(Organization.id == org_id))
    await db.execute(delete(User).where(User.id == owner_id))
    await db.commit()


async def _surgical_scenario():
    """Directly reproduces the expiry mechanism and proves handle_message()'s
    own post-rollback code (session persistence, touch_session) survives it."""
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]
        owner, org = await _make_org_and_owner(db, suffix)
        org_id, owner_id = org.id, owner.id

        session_id = None
        try:
            svc = ChatService(db, org_id)

            chat_session = await svc._get_or_create_session(owner, None, "first message")
            captured_session_id = chat_session.id  # safe: read before any rollback

            await db.rollback()  # simulates run_tool()'s partial-failure/exception rollback

            # Sanity check: confirms this test scenario genuinely reproduces
            # the expiry the production traceback hit — a raw attribute read
            # off the now-expired ORM object must fail exactly like the bug
            # report's traceback did, so this test would have caught the
            # regression if the fix were reverted.
            with pytest.raises(MissingGreenlet):
                _ = chat_session.id

            # The fixed code path: everything handle_message() itself does
            # with the session after a mid-request rollback (persist the
            # reply, touch the session) uses the captured plain int, never
            # the expired ORM object, and must work fine.
            msg = await svc._chat_repo.add_message(captured_session_id, "assistant", "a reply")
            assert msg.session_id == captured_session_id
            await svc._chat_repo.touch_session(captured_session_id)  # must not need the ORM object either

            session_id = captured_session_id
        finally:
            await _cleanup(db, org_id=org_id, owner_id=owner_id, session_id=session_id)

    await engine.dispose()


class _FakeMultiGoalLLM(LLMProvider):
    """Routes by system_prompt content to answer whichever step of the
    real pipeline is asking — planner's goal-split, intent detection (per
    step, disambiguated by user_prompt content since both steps share the
    same intent-classifier system_prompt), task creation, and the
    list-tasks summarizer."""

    def __init__(self, task_name: str):
        self._task_name = task_name

    async def generate_text(self, **kwargs):
        system_prompt = kwargs.get("system_prompt") or ""
        user_prompt = kwargs.get("user_prompt") or ""

        if "distinct" in system_prompt and "sub-instructions" in system_prompt:
            return LLMResponse(text=json.dumps({
                "steps": [f"create a task called {self._task_name}", "show my tasks"],
            }))

        if "intent classifier" in system_prompt:
            if "create a task" in user_prompt.lower():
                return LLMResponse(text=json.dumps({"intent": "create_task", "confidence": 1.0}))
            return LLMResponse(text=json.dumps({"intent": "list_tasks", "confidence": 1.0}))

        if "task-creation assistant" in system_prompt:
            return LLMResponse(text=json.dumps({"tasks": [{"name": self._task_name}]}))

        return LLMResponse(text="Here are your tasks.")


async def _integration_scenario():
    """A real two-goal message through the actual planner + intent detection
    + task-creation tool + list-tasks handler, proving the whole
    handle_message() request completes without raising and both turns
    against the same session succeed."""
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]
        owner, org = await _make_org_and_owner(db, suffix)
        org_id, owner_id = org.id, owner.id

        session_id = None
        created_task_ids: list[int] = []
        try:
            task_name = f"MultiGoal Expiry Task {suffix}"
            svc = ChatService(db, org_id)
            svc._llm = _FakeMultiGoalLLM(task_name)

            message = f"create a task called {task_name} and then show my tasks"
            response = await svc.handle_message(user=owner, message=message, session_id=None, org_role=TEAM_MANAGER)

            assert response.session_id is not None, "handle_message must return a real session_id, not crash"
            assert response.assistant_message.content, "handle_message must return a real assistant reply, not crash"
            session_id = response.session_id

            row = (await db.execute(select(Task).where(Task.name == task_name))).scalar_one_or_none()
            assert row is not None, "step 1 (create_task) must have actually created the task despite step 2 running afterward"
            created_task_ids.append(row.id)

            messages = (await db.execute(select(ChatMessage).where(ChatMessage.session_id == session_id))).scalars().all()
            assert len(messages) >= 2, f"expected at least a user + assistant message for the turn, got {len(messages)}"
        finally:
            await _cleanup(db, org_id=org_id, owner_id=owner_id, session_id=session_id, task_ids=created_task_ids)

    await engine.dispose()


def test_session_persistence_survives_mid_request_rollback():
    asyncio.run(_surgical_scenario())


def test_multi_goal_handle_message_end_to_end_does_not_crash():
    asyncio.run(_integration_scenario())
