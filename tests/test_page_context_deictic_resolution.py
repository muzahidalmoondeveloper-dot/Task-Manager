"""Validated UI/page context regression test (architecture item 9 — "no
silent fallback" companion: deictic references resolve deterministically to
what the frontend says the user is looking at, never fuzzy-guessed).

Language-agnostic refactor note: the extraction LLM now classifies the
reference into a canonical reference_type ("deictic"/"explicit"/...) itself,
in whatever language the user wrote — chat_service.py never pattern-matches
the reference text against a word list (English or otherwise) to decide
whether it's deictic. This test drives that canonical classification
directly via the scripted LLM below, proving the backend's page-context
resolution mechanism is correct independent of language.

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.

Covers: reference_type="deictic" with page_context pointing at a specific
task resolves to exactly that task, bypassing name-matching entirely; the
same reference_type with NO page_context falls back to no resolution at all
(proving the deictic shortcut isn't just always-on fuzzy leniency); an
EXPLICITLY named task (reference_type="explicit") always wins over
page_context, even when the two disagree.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete

from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import TEAM_MANAGER
from app.models.organization import Organization, OrganizationMembership
from app.models.task import Task
from app.models.user import User
from app.schemas.chat import PageContext
from app.schemas.task import TaskCreate
from app.services.chat_service import ChatService
from app.services.llm.base import LLMProvider


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="PageContext Test Owner", email=f"pagectx.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        db.add(owner)
        await db.flush()

        org = Organization(name=f"PageContext Test Org {suffix}", slug=f"pagectx-test-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.flush()
        db.add(OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner"))
        await db.commit()

        svc = ChatService(db, org.id)
        # Deliberately dissimilar names — resolve_task_reference's
        # trigram-similarity resolver correctly treats near-identical names
        # (e.g. differing by a single character) as AMBIGUOUS, which is not
        # what this test is exercising.
        task_a = await svc._task_repo.create(TaskCreate(name=f"Redesign onboarding flow {suffix}"), created_by_id=owner.id)
        task_b = await svc._task_repo.create(TaskCreate(name=f"Fix invoice export bug {suffix}"), created_by_id=owner.id)

        try:
            # ── reference_type="deictic" + page_context pointing at task A -> resolves to task A ──
            svc._page_context = PageContext(page_type="task", entity_id=task_a.id)
            svc._llm = _FakeUpdateLLM(reference_type="deictic", status="done")
            reply, actions = await svc._handle_update_task(owner, "mark this done", org_role=TEAM_MANAGER)
            assert actions and actions[0].payload.get("task_id") == task_a.id, (
                f"deictic reference with page_context=task A must resolve to task A, got reply: {reply!r}"
            )
            updated_a = await svc._task_repo.get_by_id(task_a.id)
            assert updated_a.status == "done"

            # ── No page_context -> a deictic reference resolves to nothing ──
            svc._page_context = None
            svc._llm = _FakeUpdateLLM(reference_type="deictic", status="done")
            reply, actions = await svc._handle_update_task(owner, "mark this done", org_role=TEAM_MANAGER)
            assert not actions, "with no page_context, a deictic reference must not resolve to any task"

            # ── Explicit name in the message wins over page_context pointing elsewhere ──
            svc._page_context = PageContext(page_type="task", entity_id=task_a.id)
            svc._llm = _FakeUpdateLLM(reference_type="explicit", task_reference=task_b.name, status="done")
            reply, actions = await svc._handle_update_task(owner, f"mark {task_b.name} done", org_role=TEAM_MANAGER)
            assert actions and actions[0].payload.get("task_id") == task_b.id, (
                "an explicitly named task must win over page_context pointing at a different task"
            )

        finally:
            await db.execute(delete(Task).where(Task.id.in_([task_a.id, task_b.id])))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id == org.id))
            await db.execute(delete(User).where(User.id == owner.id))
            await db.commit()

    await engine.dispose()


class _FakeUpdateLLM(LLMProvider):
    # Subclasses the real LLMProvider ABC — see test_chat_service_safety.py's
    # matching comment — so it picks up generate_structured()'s base-class
    # default (chat_service.py's update-task handler now calls
    # generate_structured() for schema-validated canonical reference
    # extraction, not generate_json() + manual dict access) instead of
    # raising AttributeError, which would propagate uncaught out of this
    # test's scenario and skip its final engine.dispose(), corrupting the
    # connection pool for every later test in the same pytest process (see
    # the module docstrings on the other live-DB test files for why that
    # matters on Windows).
    def __init__(self, reference_type: str, status: str, task_reference: str | None = None, ordinal_position: int | None = None):
        import json
        self._payload = json.dumps({
            "reference_type": reference_type,
            "task_reference": task_reference,
            "ordinal_position": ordinal_position,
            "updates": {"status": status},
        })

    async def generate_text(self, **kwargs):
        from app.services.llm.base import LLMResponse
        return LLMResponse(text=self._payload)


def test_deictic_task_reference_resolves_via_page_context():
    asyncio.run(_scenario())
