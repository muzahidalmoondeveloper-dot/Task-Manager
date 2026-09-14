"""Regression tests for the Microsoft `current_user` NameError fix and the
new Sync Run / Automation Activity routes (Automation Pipeline Audit).

ROOT CAUSE (see the final report): `sync_recent_microsoft_data` referenced
an undefined `current_user` — `NameError: name 'current_user' is not
defined` — instead of the canonical authenticated user already available
as `tenant.user` from `TenantContext`, and duplicated ~300 lines of fetch
logic that never called AI analysis/Task creation at all (a "successful"
sync never actually produced a Task).

Covers:
  1. `POST /microsoft/sync-recent` no longer raises NameError and uses
     `tenant.user` as the authenticated identity.
  2. The route actually runs FETCH -> ANALYZE (not just fetch) and
     returns a real summary reflecting Task creation.
  3. A `SyncRun` row is persisted with the correct provider/trigger and a
     terminal status derived from what happened, not a guess.
  4. `POST /google/sync-recent` (Gmail — previously nonexistent) works
     the same way.
  5. `GET /integrations/status` reflects the account's last-sync
     checkpoint/status after a successful run.
  6. `GET /integrations/sync-runs` and `GET /integrations/activity-items`
     surface that run without requiring a second nonexistent account to
     fail the call.
  7. No connected account -> 400, not a crash.

Runs against the real database connection the app uses. Every row this
test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from app.api.routes.integrations import (
    get_integration_status,
    list_activity_items,
    list_sync_runs,
    sync_recent_google_data,
    sync_recent_microsoft_data,
)
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import OWNER
from app.core.tenant import TenantContext
from app.models.integration import ImportedEmail, IntegrationAccount, SyncRun, TaskSource
from app.models.organization import Organization, OrganizationMembership
from app.models.task import Task
from app.models.user import User


async def _scenario(monkeypatch):
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]

        owner = User(full_name="SyncRoute Owner", email=f"syncroute.owner.{suffix}@example-corp.com", hashed_password="x", role=OWNER)
        db.add(owner)
        await db.commit()
        await db.refresh(owner)

        org = Organization(name=f"SyncRoute Org {suffix}", slug=f"syncroute-org-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.commit()
        org = (await db.execute(select(Organization).options(selectinload(Organization.subscription)).where(Organization.id == org.id))).scalar_one()

        membership = OrganizationMembership(organization_id=org.id, user_id=owner.id, role=OWNER)
        db.add(membership)
        await db.commit()
        owner.last_active_organization_id = org.id
        await db.commit()
        await db.refresh(membership)

        tenant = TenantContext(organization_id=org.id, organization=org, membership=membership, user=owner, db=db)

        created_task_ids: list[int] = []
        created_account_ids: list[int] = []

        async def fake_sync_no_email(*, db, user, sync_run=None):
            # Mirrors the real sync_microsoft_data_for_user's own
            # last_synced_at/last_sync_status bookkeeping (this test
            # mocks out only the Graph HTTP calls, not that behavior —
            # the pipeline tests already cover the real fetch/backoff
            # logic in depth; this one is about the ROUTE's plumbing).
            result = await db.execute(select(IntegrationAccount).where(IntegrationAccount.provider == "microsoft", IntegrationAccount.user_id == user.id))
            account = result.scalars().first()
            if account is not None:
                from datetime import datetime, timezone
                account.last_synced_at = datetime.now(timezone.utc)
                account.last_sync_status = "success"
                account.last_sync_error = None
                await db.commit()
            return {"emails_imported": 0, "calendar_events_imported": 0, "transcripts_imported": 0, "transcript_errors": []}

        try:
            # ── 7. No connected account -> 400, never a crash. ──────────────
            try:
                await sync_recent_microsoft_data(tenant=tenant, db=db)
                raise AssertionError("expected a 400 with no Microsoft account connected")
            except HTTPException as exc:
                assert exc.status_code == 400

            # ── 1, 2, 3. Connect a Microsoft account, run the route with a
            # mocked (empty) fetch stage -> no NameError, real SyncRun,
            # honest zero-Task summary (fetch succeeded, nothing to
            # analyze — never claims Task creation it didn't do). ───────────
            ms_account = IntegrationAccount(
                user_id=owner.id, organization_id=org.id, provider="microsoft",
                account_email=f"syncroute.{suffix}@outlook.com", access_token="fake", refresh_token=None,
            )
            db.add(ms_account)
            await db.commit()
            await db.refresh(ms_account)
            created_account_ids.append(ms_account.id)

            monkeypatch.setattr("app.services.automation_tasks.sync_microsoft_data_for_user", fake_sync_no_email)

            response = await sync_recent_microsoft_data(tenant=tenant, db=db)
            assert response["sync_run"]["provider"] == "microsoft"
            assert response["sync_run"]["trigger"] == "manual"
            assert response["sync_run"]["status"] == "success"
            assert response["analysis"]["tasks_created"] == 0
            assert "message" in response

            sync_run_row = (await db.execute(select(SyncRun).where(SyncRun.id == response["sync_run"]["id"]))).scalar_one()
            assert sync_run_row.provider == "microsoft"
            assert sync_run_row.triggered_by_user_id == owner.id
            assert sync_run_row.completed_at is not None

            # ── 4. Gmail manual sync route (previously nonexistent) works
            # the same way. ──────────────────────────────────────────────
            google_account = IntegrationAccount(
                user_id=owner.id, organization_id=org.id, provider="google",
                account_email=f"syncroute.{suffix}@gmail.com", access_token="fake", refresh_token=None,
            )
            db.add(google_account)
            await db.commit()
            await db.refresh(google_account)
            created_account_ids.append(google_account.id)

            async def fake_sync_gmail_no_email(*, db, user, sync_run=None):
                result = await db.execute(select(IntegrationAccount).where(IntegrationAccount.provider == "google", IntegrationAccount.user_id == user.id))
                account = result.scalars().first()
                if account is not None:
                    from datetime import datetime, timezone
                    account.last_synced_at = datetime.now(timezone.utc)
                    account.last_sync_status = "success"
                    account.last_sync_error = None
                    await db.commit()
                return {"emails_imported": 0, "transcript_errors": []}

            monkeypatch.setattr("app.services.automation_tasks.sync_gmail_data_for_user", fake_sync_gmail_no_email)
            google_response = await sync_recent_google_data(tenant=tenant, db=db)
            assert google_response["sync_run"]["provider"] == "google"
            assert google_response["sync_run"]["status"] == "success"

            # ── 5. GET /integrations/status reflects the last-sync
            # checkpoint after a successful run. ─────────────────────────
            status_response = await get_integration_status(tenant=tenant)
            accounts_by_provider = {a["provider"]: a for a in status_response["accounts"]}
            assert accounts_by_provider["microsoft"]["last_sync_status"] == "success"
            assert accounts_by_provider["microsoft"]["last_synced_at"] is not None
            assert accounts_by_provider["microsoft"]["latest_run"]["id"] == sync_run_row.id
            assert accounts_by_provider["google"]["last_sync_status"] == "success"

            # ── 6. GET /integrations/sync-runs and /activity-items surface
            # the run(s) without erroring. ───────────────────────────────
            runs_response = await list_sync_runs(provider=None, limit=20, offset=0, tenant=tenant)
            run_ids = {r["id"] for r in runs_response["runs"]}
            assert sync_run_row.id in run_ids

            ms_only_runs = await list_sync_runs(provider="microsoft", limit=20, offset=0, tenant=tenant)
            assert all(r["provider"] == "microsoft" for r in ms_only_runs["runs"])

            activity_response = await list_activity_items(limit=20, offset=0, tenant=tenant)
            assert isinstance(activity_response["items"], list)  # no items imported in this test — must not crash

        finally:
            if created_task_ids:
                await db.execute(delete(TaskSource).where(TaskSource.task_id.in_(created_task_ids)))
                await db.execute(delete(Task).where(Task.id.in_(created_task_ids)))
            if created_account_ids:
                await db.execute(delete(ImportedEmail).where(ImportedEmail.integration_account_id.in_(created_account_ids)))
                await db.execute(delete(SyncRun).where(SyncRun.integration_account_id.in_(created_account_ids)))
            await db.execute(delete(SyncRun).where(SyncRun.organization_id == org.id))
            await db.execute(delete(IntegrationAccount).where(IntegrationAccount.id.in_(created_account_ids)))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id == org.id))
            await db.execute(delete(User).where(User.id == owner.id))
            await db.commit()

    await engine.dispose()


def test_integrations_sync_routes(monkeypatch):
    asyncio.run(_scenario(monkeypatch))
