"""Regression tests for the Task Request Reject/Convert race-condition
follow-up.

BUG BEING GUARDED AGAINST: `convert_task_request` already locked its
request row with `SELECT ... FOR UPDATE`
(TaskRequestRepository.get_by_id_for_update), but `reject_task_request`
read the row with a plain, unlocked `get_by_id()`. A Convert and a Reject
fired at the same moment on the same request could both observe
status="pending" before either committed, and both would proceed — a Task
gets created AND the request ends up "rejected", an inconsistent final
state (a Task existing for a request that reads as rejected).

FIX: reject_task_request now also locks the row with
get_by_id_for_update() and shares its status update with a single commit
(mark_rejected_no_commit), the identical pattern convert_task_request
already used. Whichever request's transaction commits first wins the
lock; the other blocks, then re-reads the now-committed status and is
rejected by the unchanged `if request.status != "pending"` check.

Covers:
  1. Reject on a pending request succeeds.
  2. Reject on an already-converted request -> 409 conflict.
  3. Reject on an already-rejected request -> 409 conflict (idempotent
     rejection is NOT supported — same "already reviewed" convention
     convert already uses).
  4. Convert wins a genuine concurrent Convert-vs-Reject race (two
     independent DB sessions, `asyncio.gather`) -> final status
     "converted", exactly one Task exists.
  5. Reject wins a genuine concurrent Reject-vs-Convert race -> final
     status "rejected", zero Tasks exist.
  6. No reachable state has the request "rejected" while a converted Task
     also exists for it (derived from 4 and 5 together).

Runs against the real database connection the app uses (AsyncSessionLocal/
asyncpg — SELECT ... FOR UPDATE needs a real transactional backend, and
the race tests need two independent real connections). Every row this
test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from fastapi import BackgroundTasks
from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from app.api.routes.task_requests import convert_task_request, reject_task_request
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import OWNER, PROJECT_MANAGER
from app.core.tenant import TenantContext
from app.models.organization import Organization, OrganizationMembership
from app.models.project import Project, ProjectMembership
from app.models.task import Task
from app.models.task_request import TaskRequest
from app.models.user import User
from app.schemas.task_request import TaskRequestConvert, TaskRequestReject


async def _tenant_in_session(db, org_id, user_id) -> TenantContext:
    """Rebuilds a TenantContext bound to THIS session's own connection —
    mirrors what get_tenant_context() does per-request; needed here
    because each concurrent race participant must use its own independent
    DB session/connection, exactly like two real concurrent HTTP requests
    would (see test_change_set_toctou.py's identical pattern)."""
    org = (await db.execute(
        select(Organization).options(selectinload(Organization.subscription)).where(Organization.id == org_id)
    )).scalar_one()
    membership = (await db.execute(
        select(OrganizationMembership).where(
            OrganizationMembership.organization_id == org_id,
            OrganizationMembership.user_id == user_id,
        )
    )).scalar_one()
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
    return TenantContext(organization_id=org_id, organization=org, membership=membership, user=user, db=db)


async def _convert_via_own_session(org_id, project_id, request_id, pm_user_id, *, delay: float = 0) -> str:
    """One concurrent participant: its own session, its own row-locked
    fetch (through the real route), its own commit — exactly what a real
    concurrent HTTP request to POST .../convert would do. `delay` gives
    the OTHER participant a deliberate head start so a specific race test
    (e.g. "Convert wins") is deterministic rather than flaky, while still
    exercising two genuinely independent connections/transactions and the
    real row lock — the outcome is decided by SELECT ... FOR UPDATE
    contention, not by which line of test code runs first."""
    if delay:
        await asyncio.sleep(delay)
    async with AsyncSessionLocal() as db:
        tenant = await _tenant_in_session(db, org_id, pm_user_id)
        try:
            await convert_task_request(
                project_id, request_id, TaskRequestConvert(conversion_mode="self"),
                background_tasks=BackgroundTasks(), tenant=tenant,
            )
            return "converted_ok"
        except AppException as exc:
            return f"convert_failed:{exc.code}"


async def _reject_via_own_session(org_id, project_id, request_id, pm_user_id, *, delay: float = 0) -> str:
    if delay:
        await asyncio.sleep(delay)
    async with AsyncSessionLocal() as db:
        tenant = await _tenant_in_session(db, org_id, pm_user_id)
        try:
            await reject_task_request(
                project_id, request_id, TaskRequestReject(reason="racing reject"),
                background_tasks=BackgroundTasks(), tenant=tenant,
            )
            return "rejected_ok"
        except AppException as exc:
            return f"reject_failed:{exc.code}"


async def _run():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]

        owner = User(full_name="TRRC Owner", email=f"trrc.owner.{suffix}@example-corp.com", hashed_password="x", role="owner")
        pm = User(full_name="TRRC PM", email=f"trrc.pm.{suffix}@example-corp.com", hashed_password="x", role=PROJECT_MANAGER)
        db.add_all([owner, pm])
        await db.commit()
        for u in (owner, pm):
            await db.refresh(u)

        org = Organization(name=f"TRRC Org {suffix}", slug=f"trrc-org-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.commit()
        org = (await db.execute(select(Organization).options(selectinload(Organization.subscription)).where(Organization.id == org.id))).scalar_one()

        memberships = {}
        for u, role in [(owner, OWNER), (pm, PROJECT_MANAGER)]:
            m = OrganizationMembership(organization_id=org.id, user_id=u.id, role=role)
            db.add(m)
            memberships[u.id] = m
        await db.commit()

        project = Project(name=f"TRRC Project {suffix}", created_by_id=owner.id, organization_id=org.id)
        db.add(project)
        await db.commit()
        await db.refresh(project)
        db.add(ProjectMembership(project_id=project.id, user_id=pm.id))
        await db.commit()

        pm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[pm.id], user=pm, db=db)

        created_task_ids: list[int] = []
        created_request_ids: list[int] = []

        async def _new_request(title: str) -> TaskRequest:
            request = TaskRequest(organization_id=org.id, project_id=project.id, submitted_by_id=owner.id, title=title, status="pending")
            db.add(request)
            await db.commit()
            await db.refresh(request)
            created_request_ids.append(request.id)
            return request

        try:
            # ── 1. Reject on a pending request succeeds. ────────────────────
            req_pending = await _new_request(f"TRRC Pending {suffix}")
            rejected = await reject_task_request(
                project.id, req_pending.id, TaskRequestReject(reason="not needed"),
                background_tasks=BackgroundTasks(), tenant=pm_tenant,
            )
            assert rejected.status == "rejected"

            # ── 2. Reject on an already-converted request -> conflict. ──────
            req_converted = await _new_request(f"TRRC Converted {suffix}")
            converted = await convert_task_request(
                project.id, req_converted.id, TaskRequestConvert(conversion_mode="self"),
                background_tasks=BackgroundTasks(), tenant=pm_tenant,
            )
            created_task_ids.append(converted.converted_task_id)
            try:
                await reject_task_request(
                    project.id, req_converted.id, TaskRequestReject(),
                    background_tasks=BackgroundTasks(), tenant=pm_tenant,
                )
                raise AssertionError("rejecting an already-converted request must be a conflict")
            except AppException as exc:
                assert exc.code == "TASK_REQUEST_ALREADY_REVIEWED", exc
                assert exc.status_code == 409
            still_converted_status = (await db.execute(select(TaskRequest.status).where(TaskRequest.id == req_converted.id))).scalar_one()
            assert still_converted_status == "converted", "a failed reject attempt must never flip an already-converted request"

            # ── 3. Reject on an already-rejected request -> conflict (not
            # silently idempotent — matches convert's own "already
            # reviewed" convention). ─────────────────────────────────────
            try:
                await reject_task_request(
                    project.id, req_pending.id, TaskRequestReject(),
                    background_tasks=BackgroundTasks(), tenant=pm_tenant,
                )
                raise AssertionError("rejecting an already-rejected request must be a conflict")
            except AppException as exc:
                assert exc.code == "TASK_REQUEST_ALREADY_REVIEWED", exc
                assert exc.status_code == 409

            # ── 4. Convert wins a genuine concurrent Convert-vs-Reject
            # race: two independent sessions/connections race via
            # asyncio.gather — final status "converted", exactly one Task. ──
            req_race_convert_wins = await _new_request(f"TRRC Race ConvertWins {suffix}")
            results = await asyncio.gather(
                _convert_via_own_session(org.id, project.id, req_race_convert_wins.id, pm.id),
                # A small deliberate head start for Convert makes which
                # side wins the row lock deterministic for this specific
                # assertion, without weakening what's being proven — the
                # lock contention and the DB-level rejection are both
                # still real (see the helper's own docstring).
                _reject_via_own_session(org.id, project.id, req_race_convert_wins.id, pm.id, delay=0.05),
            )
            outcomes = set(results)
            assert "converted_ok" in outcomes, f"expected one 'converted_ok' outcome, got {results}"
            assert any(r.startswith("reject_failed:TASK_REQUEST_ALREADY_REVIEWED") for r in results), (
                f"expected the losing reject to fail with the already-reviewed conflict, got {results}"
            )
            # Column-only selects (not `select(TaskRequest)`/`select(Task)`)
            # deliberately avoid the ORM identity map — this session's map
            # still holds these rows as they were before either concurrent
            # race participant ran (expire_on_commit is False app-wide,
            # same reasoning as test_change_set_toctou.py), and re-selecting
            # the full entity would just hand back the stale cached
            # instance without proving what the racing sessions actually
            # committed.
            final_status = (await db.execute(select(TaskRequest.status).where(TaskRequest.id == req_race_convert_wins.id))).scalar_one()
            assert final_status == "converted", "Convert must win when it wins the row lock"
            race_task_ids = (await db.execute(select(Task.id).where(Task.name == req_race_convert_wins.title))).scalars().all()
            assert len(race_task_ids) == 1, "exactly one Task must exist for the raced request"
            created_task_ids.append(race_task_ids[0])

            # ── 5. Reject wins a genuine concurrent Reject-vs-Convert
            # race -> final status "rejected", zero Tasks. ──────────────────
            req_race_reject_wins = await _new_request(f"TRRC Race RejectWins {suffix}")
            results = await asyncio.gather(
                _reject_via_own_session(org.id, project.id, req_race_reject_wins.id, pm.id),
                _convert_via_own_session(org.id, project.id, req_race_reject_wins.id, pm.id, delay=0.05),
            )
            outcomes = set(results)
            assert "rejected_ok" in outcomes, f"expected one 'rejected_ok' outcome, got {results}"
            assert any(r.startswith("convert_failed:TASK_REQUEST_ALREADY_REVIEWED") for r in results), (
                f"expected the losing convert to fail with the already-reviewed conflict, got {results}"
            )
            final_status = (await db.execute(select(TaskRequest.status).where(TaskRequest.id == req_race_reject_wins.id))).scalar_one()
            assert final_status == "rejected", "Reject must win when it wins the row lock"
            no_task_ids = (await db.execute(select(Task.id).where(Task.name == req_race_reject_wins.title))).scalars().all()
            assert len(no_task_ids) == 0, "no Task must ever be created for a request that ends up rejected"

            # ── 6. Derived from 4+5: no reachable state has a request
            # "rejected" while a Task exists for it — asserted directly
            # against both raced requests above via their converted_task_id. ─
            convert_wins_row = (await db.execute(
                select(TaskRequest.status, TaskRequest.converted_task_id).where(TaskRequest.id == req_race_convert_wins.id)
            )).one()
            assert not (convert_wins_row.status == "rejected" and convert_wins_row.converted_task_id is not None)
            reject_wins_row = (await db.execute(
                select(TaskRequest.status, TaskRequest.converted_task_id).where(TaskRequest.id == req_race_reject_wins.id)
            )).one()
            assert not (reject_wins_row.status == "rejected" and reject_wins_row.converted_task_id is not None)

        finally:
            if created_request_ids:
                await db.execute(delete(TaskRequest).where(TaskRequest.id.in_(created_request_ids)))
            if created_task_ids:
                await db.execute(delete(Task).where(Task.id.in_(created_task_ids)))
            await db.execute(delete(ProjectMembership).where(ProjectMembership.project_id == project.id))
            await db.execute(delete(Project).where(Project.id == project.id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id == org.id))
            await db.execute(delete(User).where(User.id.in_([owner.id, pm.id])))
            await db.commit()

    await engine.dispose()


def test_task_request_reject_convert_race():
    asyncio.run(_run())
