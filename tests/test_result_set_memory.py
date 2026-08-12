"""Previous result-set memory + deterministic positional follow-up
regression test (architecture item 8, language-agnostic refactor).

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.

Covers: after a list-type reply, the ordered task-id list is stored on the
session; a follow-up whose extraction LLM classified reference_type="ordinal"
with ordinal_position=2 resolves deterministically to the second id in that
stored list (not the first, not a fuzzy guess); ordinal_position=-1 ("the
last one") resolves to the final id; an out-of-range ordinal_position (5
against a 3-item list) fails explicitly rather than clamping to the nearest
valid item; and a session with no stored result set at all never resolves an
ordinal position.

Language-agnostic refactor note: _resolve_positional_task_id() used to
receive a raw reference STRING ("the second one", "3rd", ...) and pattern-
match it against a hardcoded English word list — this test used to assert
that behavior directly. The LLM now performs that classification itself (in
whatever language the user wrote) and hands the backend a canonical integer
position; _resolve_positional_task_id() only ever indexes into the stored
result set with that integer. This test proves the (language-independent)
integer-indexing mechanism; test_bengali_banglish_reference_understanding.py
proves the LLM's classification step end-to-end for non-English input.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete

from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import TEAM_MANAGER
from app.models.chat import ChatSession
from app.models.organization import Organization, OrganizationMembership
from app.models.task import Task
from app.models.user import User
from app.schemas.task import TaskCreate
from app.services.chat_service import ChatService
from app.services.llm.base import LLMProvider


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="ResultSet Test Owner", email=f"resultset.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        db.add(owner)
        await db.flush()

        org = Organization(name=f"ResultSet Test Org {suffix}", slug=f"resultset-test-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.flush()
        db.add(OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner"))
        session = ChatSession(user_id=owner.id, organization_id=org.id)
        db.add(session)
        await db.commit()
        await db.refresh(session)

        svc = ChatService(db, org.id)
        task_1 = await svc._task_repo.create(TaskCreate(name=f"Alpha result-set task {suffix}"), created_by_id=owner.id)
        task_2 = await svc._task_repo.create(TaskCreate(name=f"Bravo result-set task {suffix}"), created_by_id=owner.id)
        task_3 = await svc._task_repo.create(TaskCreate(name=f"Charlie result-set task {suffix}"), created_by_id=owner.id)

        try:
            # ── Store a result set (simulating what _handle_list_tasks does after showing a list) ──
            await svc._store_result_set(session.id, "task", [task_1.id, task_2.id, task_3.id])

            # ── ordinal_position=2 -> task_2 (canonical, 1-based) ──
            resolved = await svc._resolve_positional_task_id(session.id, 2)
            assert resolved == task_2.id, f"ordinal_position=2 must resolve to task_2 ({task_2.id}), got {resolved}"

            # ── ordinal_position=3 -> task_3 ──
            resolved = await svc._resolve_positional_task_id(session.id, 3)
            assert resolved == task_3.id

            # ── ordinal_position=-1 ("the last one") -> task_3 (final item) ──
            resolved = await svc._resolve_positional_task_id(session.id, -1)
            assert resolved == task_3.id

            # ── out-of-range ordinal -> explicit failure (None), not clamped ──
            resolved = await svc._resolve_positional_task_id(session.id, 5)
            assert resolved is None, "an out-of-range ordinal must fail explicitly, not clamp to the nearest item"

            # ── ordinal_position=None (LLM did not classify this as an ordinal reference) -> None ──
            resolved = await svc._resolve_positional_task_id(session.id, None)
            assert resolved is None, "no ordinal_position must never resolve positionally"

            # ── end-to-end: _handle_update_task with a canonical ordinal extraction actually updates task_1 ──
            svc._llm = _FakeUpdateLLM(reference_type="ordinal", ordinal_position=1, status="done")
            reply, actions = await svc._handle_update_task(owner, "mark the first one done", session_id=session.id, org_role=TEAM_MANAGER)
            assert actions and actions[0].payload.get("task_id") == task_1.id, (
                f"ordinal_position=1 end-to-end must update task_1, got reply: {reply!r}"
            )
            updated = await svc._task_repo.get_by_id(task_1.id)
            assert updated.status == "done"

            # ── a session with no stored result set never resolves an ordinal ──
            other_session = ChatSession(user_id=owner.id, organization_id=org.id)
            db.add(other_session)
            await db.commit()
            await db.refresh(other_session)
            try:
                resolved = await svc._resolve_positional_task_id(other_session.id, 1)
                assert resolved is None, "a session with no stored result set must never resolve an ordinal"
            finally:
                await db.execute(delete(ChatSession).where(ChatSession.id == other_session.id))
                await db.commit()

        finally:
            await db.execute(delete(Task).where(Task.id.in_([task_1.id, task_2.id, task_3.id])))
            await db.execute(delete(ChatSession).where(ChatSession.id == session.id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id == org.id))
            await db.execute(delete(User).where(User.id == owner.id))
            await db.commit()

    await engine.dispose()


class _FakeUpdateLLM(LLMProvider):
    # See test_page_context_deictic_resolution.py's matching comment —
    # subclassing LLMProvider is required so generate_structured() (which
    # chat_service.py's update-task handler now calls, for schema-validated
    # canonical reference extraction) is available via the base class's
    # default implementation instead of raising AttributeError.
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


def test_positional_followup_resolves_deterministically_against_stored_result_set():
    asyncio.run(_scenario())
