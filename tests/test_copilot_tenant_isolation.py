"""Cross-tenant isolation regression test (architecture Section 4.2 /
non-negotiable rule #2: "No global cross-tenant user lookup is exposed to
chatbot reasoning").

Runs against the real database connection the app uses (AsyncSessionLocal)
— same convention as this session's live-DB verification scripts, since
this repo has no isolated test-DB/transaction-rollback fixture. Every row
this test creates is deleted before it returns, success or failure alike.

Deliberately a single `asyncio.run(...)` call for the whole scenario
(setup + assertions + teardown) rather than a pytest fixture split across
multiple `asyncio.run()` calls — the SQLAlchemy async engine's pooled
asyncpg connections are bound to the event loop they were first used on;
tearing one down and starting a fresh one per fixture/test function (as a
naive `@pytest.fixture` using `asyncio.run()` internally would) corrupts
pooled connection state ("cannot perform operation: another operation is
in progress"). One event loop for the whole scenario avoids that entirely.

BUG BEING GUARDED AGAINST: app/services/chat_service.py used to build its
user list (assignee resolution, "Known system users" LLM prompt block,
user_count/user_list/user_by_role db_query sub-intents) via
UserRepository.list_all()/.list_by_roles(), both of which have NO
organization filter at all — every organization on the platform could see
and even assign tasks to every other organization's users through chat.
The fix routes all of that through UserRepository.list_by_org() /
.list_by_org_all() / .list_by_org_and_roles(), which this test exercises
directly against two real, isolated organizations.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship;
# without this, cross-referencing relationships defined on models this test
# doesn't import directly (e.g. OrganizationInvitation -> OnboardingTemplate)
# fail to configure when the mapper registry is first used.
from sqlalchemy import delete

from app.core.database import AsyncSessionLocal, engine
from app.models.organization import Organization, OrganizationMembership
from app.models.user import User
from app.repositories.user_repository import UserRepository


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        # Organizations.owner_id is NOT NULL, so users must exist first.
        user_a1 = User(full_name="Org A Owner", email=f"orga.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        user_a2 = User(full_name="Org A Manager", email=f"orga.mgr.{suffix}@test.invalid", hashed_password="x", role="team_manager")
        user_b1 = User(full_name="Org B Owner", email=f"orgb.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        db.add_all([user_a1, user_a2, user_b1])
        await db.flush()

        org_a = Organization(name=f"Tenant Test Org A {suffix}", slug=f"tenant-test-a-{suffix}", owner_id=user_a1.id)
        org_b = Organization(name=f"Tenant Test Org B {suffix}", slug=f"tenant-test-b-{suffix}", owner_id=user_b1.id)
        db.add_all([org_a, org_b])
        await db.flush()

        db.add_all([
            OrganizationMembership(organization_id=org_a.id, user_id=user_a1.id, role="owner"),
            OrganizationMembership(organization_id=org_a.id, user_id=user_a2.id, role="team_manager"),
            OrganizationMembership(organization_id=org_b.id, user_id=user_b1.id, role="owner"),
        ])
        await db.commit()

        try:
            repo = UserRepository(db)

            # ── list_by_org(): active-member scoping (assignee resolution) ──
            org_a_users = await repo.list_by_org(org_a.id)
            org_b_users = await repo.list_by_org(org_b.id)
            org_a_ids = {u.id for u in org_a_users}
            org_b_ids = {u.id for u in org_b_users}
            assert {user_a1.id, user_a2.id} <= org_a_ids, "org A must see both of its own members"
            assert {user_b1.id} <= org_b_ids, "org B must see its own member"
            assert user_b1.id not in org_a_ids, "CROSS-TENANT LEAK: org B's user visible in org A's scoped list"
            assert user_a1.id not in org_b_ids, "CROSS-TENANT LEAK: org A's user visible in org B's scoped list"
            assert user_a2.id not in org_b_ids, "CROSS-TENANT LEAK: org A's user visible in org B's scoped list"

            # ── list_by_org_all(): includes inactive, still org-scoped ──
            org_a_members = await repo.list_by_org_all(org_a.id)
            org_b_members = await repo.list_by_org_all(org_b.id)
            org_a_member_ids = {u.id for u, _m in org_a_members}
            org_b_member_ids = {u.id for u, _m in org_b_members}
            assert user_b1.id not in org_a_member_ids
            assert user_a1.id not in org_b_member_ids

            # ── list_by_org_and_roles(): role filter stays within the org ──
            org_a_owners = await repo.list_by_org_and_roles(org_a.id, ["owner"])
            org_b_owners = await repo.list_by_org_and_roles(org_b.id, ["owner"])
            assert {u.id for u in org_a_owners} == {user_a1.id}
            assert {u.id for u in org_b_owners} == {user_b1.id}

            # ── Documents WHY the fix was necessary: the raw global method
            #    has zero org filtering, so both orgs' users appear in it
            #    together — exactly what chat_service.py no longer calls
            #    for anything user-facing. ──
            all_ids = {u.id for u in await repo.list_all()}
            assert {user_a1.id, user_a2.id, user_b1.id} <= all_ids

        finally:
            # Organizations.owner_id -> users.id is ON DELETE RESTRICT, so
            # the org rows must go before the user rows they reference.
            await db.execute(delete(OrganizationMembership).where(
                OrganizationMembership.organization_id.in_([org_a.id, org_b.id])
            ))
            await db.execute(delete(Organization).where(Organization.id.in_([org_a.id, org_b.id])))
            await db.execute(delete(User).where(User.id.in_([user_a1.id, user_a2.id, user_b1.id])))
            await db.commit()

    # See test_chat_service_safety.py's matching comment — disposing here,
    # on this same event loop, prevents a pooled connection from this loop
    # being reused by a later independent asyncio.run() in another test file.
    await engine.dispose()


def test_user_repository_enforces_tenant_isolation():
    asyncio.run(_scenario())
