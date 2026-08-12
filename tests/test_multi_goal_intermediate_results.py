"""Multi-goal planning intermediate-result propagation regression test
(architecture item 11).

BUG BEING GUARDED AGAINST: planner.py splits a message like "create a task
for the launch and then assign it to Sarah" into two independent,
self-contained steps ("create a task for the launch" / "assign it to
Sarah") run in sequence. Before this fix, step 2 had no way to know WHICH
task step 1 had just created — "it" would fail to resolve (no page context,
no stored result set matching an ordinal) and the user would get "I
couldn't find a task matching 'it'" immediately after the assistant had
just told them the task was created.

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.
"""

import asyncio
import json
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete

from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import TEAM_MANAGER
from app.models.organization import Organization, OrganizationMembership
from app.models.task import Task
from app.models.user import User
from app.services.chat_service import ChatService
from app.services.llm.base import LLMProvider, LLMResponse


class _FakeCreateThenUpdateLLM(LLMProvider):
    """Scripted to answer whatever the current step asks for — a real
    per-step LLM call would receive a different, self-contained prompt for
    each step (that's what the planner guarantees), but for this test we
    just need step 1's extraction to name a task and step 2's extraction to
    classify "it" as a canonical deictic reference (language-agnostic
    refactor — see chat_service._resolve_task_reference()'s docstring: the
    LLM performs this classification itself, the backend never pattern-
    matches "it" or any other pronoun word directly)."""

    def __init__(self, task_name: str):
        self._task_name = task_name

    async def generate_text(self, **kwargs):
        # The two handlers use distinct system prompts (_CREATE_TASK_SYSTEM
        # vs _UPDATE_TASK_SYSTEM) — routing on that is more robust than
        # guessing at user-prompt wording.
        system_prompt = kwargs.get("system_prompt", "") or ""
        if "update" in system_prompt.lower() or "reference_type" in system_prompt.lower():
            return LLMResponse(text=json.dumps({
                "reference_type": "deictic",
                "task_reference": None,
                "ordinal_position": None,
                "updates": {"status": "done"},
            }))
        return LLMResponse(text=json.dumps({"tasks": [{"name": self._task_name}]}))


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="MultiGoal Test Owner", email=f"multigoal.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        db.add(owner)
        await db.flush()

        org = Organization(name=f"MultiGoal Test Org {suffix}", slug=f"multigoal-test-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.flush()
        db.add(OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner"))
        await db.commit()

        created_task_ids: list[int] = []
        try:
            task_name = f"Launch prep {suffix}"
            svc = ChatService(db, org.id)
            svc._llm = _FakeCreateThenUpdateLLM(task_name)

            # Step 1: create the task (mirrors what the planner's step 1 does).
            reply1, actions1 = await svc._handle_create_task(owner, f"create a task for {task_name}", org_role=TEAM_MANAGER)
            assert actions1, f"step 1 must create a task, got reply: {reply1!r}"
            task_id = actions1[0].payload["task_id"]
            created_task_ids.append(task_id)

            assert svc._step_results["task"] == task_id, "the handler must record the task it just created"

            # Step 2: "assign it to Sarah" (extraction returns task_reference="it", no page_context set)
            # — must resolve deterministically to the task step 1 just created, not fail.
            reply2, actions2 = await svc._handle_update_task(owner, "mark it done", org_role=TEAM_MANAGER)
            assert actions2 and actions2[0].payload.get("task_id") == task_id, (
                f"INTERMEDIATE-RESULT BUG: step 2's 'it' must resolve to step 1's just-created task, got reply: {reply2!r}"
            )
            updated = await svc._task_repo.get_by_id(task_id)
            assert updated.status == "done"

        finally:
            if created_task_ids:
                await db.execute(delete(Task).where(Task.id.in_(created_task_ids)))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id == org.id))
            await db.execute(delete(User).where(User.id == owner.id))
            await db.commit()

    await engine.dispose()


def test_second_step_resolves_deictic_reference_to_first_steps_created_task():
    asyncio.run(_scenario())
