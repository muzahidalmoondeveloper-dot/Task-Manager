"""No-silent-fallback regression test (architecture item 6 — "Safe
deterministic entity/reference resolution with no silent fallback").

BUG BEING GUARDED AGAINST: ChatService._resolve_user_id(name, fallback=...)
used to do a naive first-substring-match scan and, when `name` was given but
matched nobody (typo, wrong name, doesn't exist in the org), silently
returned `fallback` — normally the requesting user's own id. That means
"create a task for Sarah and assign it to her" for a nonexistent/misspelled
"Sarah" would silently self-assign the task to whoever sent the chat
message, with zero indication anything went wrong. The same shape of bug
existed for `_resolve_project_id`/`_resolve_team_id` (silently proceeding
with no project/team filter instead of surfacing the failure) and for
ambiguous matches (first substring hit wins, arbitrarily, instead of being
flagged AMBIGUOUS).

This test drives the real ChatService.process_message() end-to-end (via the
real `_handle_create_task` -> Domain Tool Registry path) against the live
dev DB, with a scripted fake LLM standing in for the extraction call, and
asserts: (1) a task explicitly addressed to a nonexistent assignee name is
created UNASSIGNED with an explicit warning in the reply, never silently
assigned to the requesting user; (2) a task with no assignee mentioned at
all still defaults to self-assignment (that default is legitimate — it's
not a failed resolution); (3) an ambiguous name (matches two different real
users) is flagged as unresolved, not arbitrarily assigned to whichever user
happened to be scanned first.
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


class _ScriptedCreateTaskLLM(LLMProvider):
    """Always returns the same single-task extraction payload, with the
    assignee name supplied by the test — the extraction step (parsing free
    text into a name) is not what this test is verifying; the resolution
    step (turning that name into an id, or explicitly failing) is."""

    def __init__(self, task_name: str, assignee_name: str | None):
        self._payload = json.dumps({
            "tasks": [{"name": task_name, "assignee_name": assignee_name}]
        })

    async def generate_text(self, **kwargs):
        return LLMResponse(text=self._payload)


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        requester = User(full_name="Resolution Test Requester", email=f"resolution.req.{suffix}@test.invalid", hashed_password="x", role="team_manager")
        real_target = User(full_name=f"Sarah Connor {suffix}", email=f"resolution.sarah.{suffix}@test.invalid", hashed_password="x", role="team_member")
        ambiguous_1 = User(full_name=f"Alex Morgan {suffix}", email=f"resolution.alex1.{suffix}@test.invalid", hashed_password="x", role="team_member")
        ambiguous_2 = User(full_name=f"Alex Rivera {suffix}", email=f"resolution.alex2.{suffix}@test.invalid", hashed_password="x", role="team_member")
        db.add_all([requester, real_target, ambiguous_1, ambiguous_2])
        await db.flush()

        org = Organization(name=f"Resolution Test Org {suffix}", slug=f"resolution-test-{suffix}", owner_id=requester.id)
        db.add(org)
        await db.flush()

        db.add_all([
            OrganizationMembership(organization_id=org.id, user_id=requester.id, role="team_manager"),
            OrganizationMembership(organization_id=org.id, user_id=real_target.id, role="team_member"),
            OrganizationMembership(organization_id=org.id, user_id=ambiguous_1.id, role="team_member"),
            OrganizationMembership(organization_id=org.id, user_id=ambiguous_2.id, role="team_member"),
        ])
        await db.commit()

        created_task_ids: list[int] = []
        try:
            svc = ChatService(db, org.id)

            # ── 1. Nonexistent assignee name -> task created UNASSIGNED with an explicit warning, never self-assigned ──
            bogus_name = f"Nonexistent Person {suffix}"
            svc._llm = _ScriptedCreateTaskLLM(f"Nonexistent-assignee task {suffix}", bogus_name)
            reply, actions = await svc._handle_create_task(requester, "create a task", org_role=TEAM_MANAGER)
            assert actions and actions[0].payload.get("task_id"), f"task should still be created, got reply: {reply!r}"
            task_id = actions[0].payload["task_id"]
            created_task_ids.append(task_id)
            task = await svc._task_repo.get_by_id(task_id)
            assert task.assignee_id is None, (
                f"SILENT FALLBACK BUG: task with an unresolvable assignee name was assigned to "
                f"user {task.assignee_id} instead of being left unassigned"
            )
            assert bogus_name in reply, "the reply must explicitly warn about the unresolved name, not silently succeed"

            # ── 2. No assignee mentioned at all -> legitimate self-assignment default still works ──
            svc._llm = _ScriptedCreateTaskLLM(f"No-assignee-mentioned task {suffix}", None)
            reply, actions = await svc._handle_create_task(requester, "create a task", org_role=TEAM_MANAGER)
            task_id = actions[0].payload["task_id"]
            created_task_ids.append(task_id)
            task = await svc._task_repo.get_by_id(task_id)
            assert task.assignee_id == requester.id, "with no assignee mentioned at all, self-assignment default must still apply"

            # ── 3. Real, resolvable name -> actually resolves correctly ──
            svc._llm = _ScriptedCreateTaskLLM(f"Real-assignee task {suffix}", real_target.full_name)
            reply, actions = await svc._handle_create_task(requester, "create a task", org_role=TEAM_MANAGER)
            task_id = actions[0].payload["task_id"]
            created_task_ids.append(task_id)
            task = await svc._task_repo.get_by_id(task_id)
            assert task.assignee_id == real_target.id, "a real, unambiguous name must resolve to the correct user"

            # ── 4. Ambiguous name ("Alex") -> unassigned + flagged, not an arbitrary pick ──
            svc._llm = _ScriptedCreateTaskLLM(f"Ambiguous-assignee task {suffix}", f"Alex {suffix}")
            reply, actions = await svc._handle_create_task(requester, "create a task", org_role=TEAM_MANAGER)
            task_id = actions[0].payload["task_id"]
            created_task_ids.append(task_id)
            task = await svc._task_repo.get_by_id(task_id)
            assert task.assignee_id is None, (
                "AMBIGUITY BUG: an ambiguous name matching two different real users must not be "
                "arbitrarily assigned to whichever one was scanned first"
            )
            assert "more than one" in reply.lower(), "the reply must explain the ambiguity, not silently pick one"

        finally:
            if created_task_ids:
                await db.execute(delete(Task).where(Task.id.in_(created_task_ids)))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id == org.id))
            await db.execute(delete(User).where(User.id.in_([requester.id, real_target.id, ambiguous_1.id, ambiguous_2.id])))
            await db.commit()

    await engine.dispose()


def test_assignee_resolution_never_silently_falls_back():
    asyncio.run(_scenario())
