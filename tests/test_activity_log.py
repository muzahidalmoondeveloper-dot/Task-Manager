"""Regression tests for Task #8 — User Activity Log
(app.models.activity_log.ActivityLog / app.services.activity_service /
app.api.routes.activity_logs / activity hooks in tasks.py, projects.py,
teams.py, users.py).

Covers (see PHASE 32 of the spec):
  1. task create produces an activity record
  2. task update produces an activity record with correct field-change metadata
  3. task delete leaves a persistent, readable audit record (entity_label
     preserved even though the Task row itself is gone)
  4. timer start produces an activity record
  5. timer stop produces an activity record with safe duration metadata
  6. project mutation (create) produces an activity record
  7. team mutation (create) produces an activity record
  8. a role change produces an activity record with from/to metadata
  9. a failed/unauthorized mutation does not produce a success record
  10. the log uses the authenticated actor, never a client-supplied identity
      (verified by construction — activity_service.record()'s signature
      only ever takes what the route already resolved server-side)
  11. organization A cannot read organization B's logs
  12. Admin/Owner can read
  13. a plain Team Manager cannot read org-wide logs
  14. a Project Manager cannot read org-wide logs
  15. pagination works (page/page_size, total reflects the full filtered set)
  16. newest-first ordering holds
  17. the action filter works
  18. a deleted task's activity history remains readable (same evidence as #3)
  19. no sensitive substrings survive the metadata sanitizer even if a
      caller (by mistake) tried to pass one
  20. there is no PATCH/DELETE route registered for activity logs at all —
      append-only is structural, not just a convention

Runs against the real database connection the app uses. Every row this
test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from fastapi import BackgroundTasks
from sqlalchemy import delete, select, text
from sqlalchemy.orm import selectinload

from app.api.routes.activity_logs import _serialize, list_activity_logs
from app.api.routes.projects import create_project
from app.api.routes.tasks import create_task, delete_task, start_task_timer, stop_task_timer, update_task
from app.api.routes.teams import create_team
from app.api.routes.users import update_user
from app.core.activity_actions import (
    PROJECT_CREATED,
    TASK_CREATED,
    TASK_DELETED,
    TASK_TIMER_STARTED,
    TASK_TIMER_STOPPED,
    TASK_UPDATED,
    TEAM_CREATED,
    USER_ROLE_CHANGED,
)
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import PROJECT_MANAGER, TEAM_MANAGER, TEAM_MEMBER
from app.core.tenant import TenantContext, require_org_admin, require_org_manager
from app.models.activity_log import ActivityLog
from app.models.organization import Organization, OrganizationMembership
from app.models.project import Project
from app.models.task import Task
from app.models.team import Team
from app.models.user import User
from app.repositories.activity_log_repository import ActivityLogRepository
from app.schemas.project import ProjectCreate
from app.schemas.task import TaskCreate, TaskUpdate
from app.schemas.team import TeamCreate
from app.schemas.user import UserUpdate
from app.services import activity_service


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:10]

        owner = User(full_name="AL Owner", email=f"al.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        member = User(full_name="AL Member", email=f"al.member.{suffix}@test.invalid", hashed_password="x", role=TEAM_MEMBER)
        tm = User(full_name="AL TM", email=f"al.tm.{suffix}@test.invalid", hashed_password="x", role=TEAM_MANAGER)
        pm = User(full_name="AL PM", email=f"al.pm.{suffix}@test.invalid", hashed_password="x", role=PROJECT_MANAGER)
        outsider = User(full_name="AL Outsider", email=f"al.outsider.{suffix}@test.invalid", hashed_password="x", role="owner")
        db.add_all([owner, member, tm, pm, outsider])
        await db.commit()
        for u in (owner, member, tm, pm, outsider):
            await db.refresh(u)

        org = Organization(name=f"AL Org {suffix}", slug=f"al-org-{suffix}", owner_id=owner.id)
        other_org = Organization(name=f"AL Other Org {suffix}", slug=f"al-other-org-{suffix}", owner_id=outsider.id)
        db.add_all([org, other_org])
        await db.commit()
        # Eager-load `subscription` (used by create_project's
        # check_active_billing) up front — a later bare `.subscription`
        # access on an object refreshed via plain db.refresh() would
        # otherwise lazy-load outside a valid async context.
        org = (await db.execute(select(Organization).options(selectinload(Organization.subscription)).where(Organization.id == org.id))).scalar_one()
        other_org = (await db.execute(select(Organization).options(selectinload(Organization.subscription)).where(Organization.id == other_org.id))).scalar_one()

        memberships = {}
        for u, role in [(owner, "owner"), (member, TEAM_MEMBER), (tm, TEAM_MANAGER), (pm, PROJECT_MANAGER)]:
            m = OrganizationMembership(organization_id=org.id, user_id=u.id, role=role)
            db.add(m)
            memberships[u.id] = m
        outsider_membership = OrganizationMembership(organization_id=other_org.id, user_id=outsider.id, role="owner")
        db.add(outsider_membership)
        await db.commit()
        for m in list(memberships.values()) + [outsider_membership]:
            await db.refresh(m)

        owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[owner.id], user=owner, db=db)
        member_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[member.id], user=member, db=db)
        tm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[tm.id], user=tm, db=db)
        pm_tenant = TenantContext(organization_id=org.id, organization=org, membership=memberships[pm.id], user=pm, db=db)
        outsider_tenant = TenantContext(organization_id=other_org.id, organization=other_org, membership=outsider_membership, user=outsider, db=db)

        created_task_ids: list[int] = []
        created_project_ids: list[int] = []
        created_team_ids: list[int] = []
        extra_user_ids: list[int] = []  # additional users created mid-scenario (deleteme, nameless, legacy_*)

        try:
            # ── 1. Task create produces an activity record. ─────────────────
            task = await create_task(
                TaskCreate(name=f"AL Task {suffix}"),
                background_tasks=BackgroundTasks(), tenant=owner_tenant, db=db,
            )
            created_task_ids.append(task.id)
            log_rows = (await db.execute(select(ActivityLog).where(ActivityLog.entity_type == "task", ActivityLog.entity_id == task.id, ActivityLog.action == TASK_CREATED))).scalars().all()
            assert len(log_rows) == 1
            assert log_rows[0].actor_user_id == owner.id, "actor must be the authenticated user, never client-supplied"
            assert log_rows[0].entity_label == task.name

            # ── 2. Task update produces an activity record with metadata. ───
            await update_task(
                task.id, TaskUpdate(status="in_progress", priority="high"),
                background_tasks=BackgroundTasks(), tenant=owner_tenant, db=db,
            )
            update_logs = (await db.execute(select(ActivityLog).where(ActivityLog.entity_type == "task", ActivityLog.entity_id == task.id, ActivityLog.action == TASK_UPDATED))).scalars().all()
            assert len(update_logs) == 1
            meta = update_logs[0].activity_metadata
            assert meta["status_from"] == "todo" and meta["status_to"] == "in_progress"
            assert meta["priority_from"] == "medium" and meta["priority_to"] == "high"
            assert set(meta["fields_changed"]) == {"status", "priority"}

            # ── 9. A no-op update (nothing actually changed) must not create
            # a phantom activity record. ─────────────────────────────────────
            before_count = len((await db.execute(select(ActivityLog).where(ActivityLog.entity_type == "task", ActivityLog.entity_id == task.id))).scalars().all())
            await update_task(task.id, TaskUpdate(status="in_progress"), background_tasks=BackgroundTasks(), tenant=owner_tenant, db=db)
            after_count = len((await db.execute(select(ActivityLog).where(ActivityLog.entity_type == "task", ActivityLog.entity_id == task.id))).scalars().all())
            assert after_count == before_count, "setting a field to its current value must not fabricate a change record"

            # ── 9 (continued). A rejected/unauthorized mutation creates no
            # success record. A plain team_member cannot even pass
            # require_org_manager for update_task. ──────────────────────────
            try:
                await require_org_manager(tenant=member_tenant)
                raise AssertionError("a plain team_member must not pass require_org_manager")
            except AppException:
                pass

            # ── 4, 5. Timer start/stop produce activity with safe metadata.
            # Assignee-Only Timer Control follow-up: `task` was created
            # unassigned above, and only the current assignee may
            # Start/Stop its timer now — assign it to owner first so this
            # (pre-existing, unrelated-to-that-follow-up) Activity Log
            # coverage keeps exercising a legitimate Start/Stop. ─────────────
            await update_task(task.id, TaskUpdate(assignee_id=owner.id), background_tasks=BackgroundTasks(), tenant=owner_tenant, db=db)
            await start_task_timer(task.id, tenant=owner_tenant)
            start_logs = (await db.execute(select(ActivityLog).where(ActivityLog.entity_type == "task", ActivityLog.entity_id == task.id, ActivityLog.action == TASK_TIMER_STARTED))).scalars().all()
            assert len(start_logs) == 1

            await stop_task_timer(task.id, tenant=owner_tenant)
            stop_logs = (await db.execute(select(ActivityLog).where(ActivityLog.entity_type == "task", ActivityLog.entity_id == task.id, ActivityLog.action == TASK_TIMER_STOPPED))).scalars().all()
            assert len(stop_logs) == 1
            assert isinstance(stop_logs[0].activity_metadata["duration_seconds"], int)
            assert stop_logs[0].activity_metadata["duration_seconds"] >= 0

            # ── 3, 18. Task delete leaves a persistent, readable record —
            # entity_label survives even though the Task row is gone. ───────
            task_name_before_delete = task.name
            await delete_task(task.id, tenant=owner_tenant)
            delete_logs = (await db.execute(select(ActivityLog).where(ActivityLog.entity_type == "task", ActivityLog.entity_id == task.id, ActivityLog.action == TASK_DELETED))).scalars().all()
            assert len(delete_logs) == 1
            assert delete_logs[0].entity_label == task_name_before_delete
            task_still_exists = await db.get(Task, task.id)
            assert task_still_exists is None, "sanity check: the task really is hard-deleted"
            created_task_ids.remove(task.id)

            # ── 6. Project mutation produces an activity record. ─────────────
            project = await create_project(ProjectCreate(name=f"AL Project {suffix}"), tenant=owner_tenant)
            created_project_ids.append(project.id)
            project_logs = (await db.execute(select(ActivityLog).where(ActivityLog.entity_type == "project", ActivityLog.entity_id == project.id, ActivityLog.action == PROJECT_CREATED))).scalars().all()
            assert len(project_logs) == 1

            # ── 7. Team mutation produces an activity record. ────────────────
            team = await create_team(TeamCreate(name=f"AL Team {suffix}", team_manager_id=owner.id, member_ids=[]), tenant=owner_tenant, db=db)
            created_team_ids.append(team.id)
            team_logs = (await db.execute(select(ActivityLog).where(ActivityLog.entity_type == "team", ActivityLog.entity_id == team.id, ActivityLog.action == TEAM_CREATED))).scalars().all()
            assert len(team_logs) == 1

            # ── 8. Role change produces an activity record with from/to. ────
            await update_user(member.id, UserUpdate(role=PROJECT_MANAGER), tenant=owner_tenant, db=db)
            role_logs = (await db.execute(select(ActivityLog).where(ActivityLog.entity_type == "user", ActivityLog.entity_id == member.id, ActivityLog.action == USER_ROLE_CHANGED))).scalars().all()
            assert len(role_logs) == 1
            assert role_logs[0].activity_metadata["from_role"] == TEAM_MEMBER
            assert role_logs[0].activity_metadata["to_role"] == PROJECT_MANAGER
            # restore for cleanliness of the rest of the scenario (not required, but tidy)
            memberships[member.id].role = TEAM_MEMBER

            # ── 12. Admin/Owner can read the log; 15/16. pagination +
            # newest-first ordering. ──────────────────────────────────────────
            page1 = await list_activity_logs(page=1, page_size=2, tenant=owner_tenant, db=db)
            assert page1.total >= 7, "at least the 7 events recorded above must be present"
            assert len(page1.items) == 2
            assert page1.items[0].created_at >= page1.items[1].created_at, "must be newest-first"

            page2 = await list_activity_logs(page=2, page_size=2, tenant=owner_tenant, db=db)
            assert page2.items[0].id != page1.items[0].id, "page 2 must not repeat page 1's rows"

            # ── 17. Action filter works. ──────────────────────────────────────
            filtered = await list_activity_logs(page=1, page_size=50, action=USER_ROLE_CHANGED, tenant=owner_tenant, db=db)
            assert filtered.total == 1
            assert all(item.action == USER_ROLE_CHANGED for item in filtered.items)

            # ── 13, 14. Team Manager / Project Manager cannot read org-wide
            # logs — the exact same gate protecting GET /users. `tenant` is
            # a `Depends(require_org_admin)` parameter on the route, which a
            # direct function call (bypassing FastAPI's own dependency
            # injection, per this suite's established convention) never
            # actually invokes — so the gate itself must be called
            # explicitly here to prove it rejects, exactly as
            # test_user_avatar_upload.py's unauthenticated check does for
            # get_current_user. ─────────────────────────────────────────────
            for blocked_tenant, label in [(tm_tenant, "Team Manager"), (pm_tenant, "Project Manager")]:
                try:
                    await require_org_admin(tenant=blocked_tenant)
                    raise AssertionError(f"a plain {label} must not pass require_org_admin")
                except AppException as exc:
                    assert exc.status_code == 403, exc

            # ── 11. Cross-tenant isolation. ────────────────────────────────────
            outsider_page = await list_activity_logs(page=1, page_size=50, tenant=outsider_tenant, db=db)
            assert outsider_page.total == 0, "an organization must never see another organization's activity"

            # Defense in depth at the repository level too.
            cross_repo = ActivityLogRepository(db, other_org.id)
            cross_items, cross_total = await cross_repo.list_page()
            assert cross_total == 0 and cross_items == []

            # ── 19. Sensitive substrings never survive the sanitizer even if
            # a caller passed them by mistake. ────────────────────────────────
            await activity_service.record(
                db, organization_id=org.id, actor=owner, action=TASK_UPDATED,
                entity_type="task", entity_id=999999, entity_label="sanitizer probe",
                metadata={"password": "hunter2", "access_token": "abc.def.ghi", "safe_field": "kept"},
            )
            probe_log = (await db.execute(select(ActivityLog).where(ActivityLog.entity_label == "sanitizer probe"))).scalars().first()
            assert probe_log is not None
            assert probe_log.activity_metadata is not None
            assert "password" not in probe_log.activity_metadata
            assert "access_token" not in probe_log.activity_metadata
            assert probe_log.activity_metadata.get("safe_field") == "kept"
            await db.execute(delete(ActivityLog).where(ActivityLog.id == probe_log.id))
            await db.commit()

            # ── 21-27. PRESERVE DELETED ACTOR IDENTITY (Task #8 follow-up):
            # actor_label is captured server-side, survives rename, survives
            # the actor account being deleted, is never spoofable, never
            # leaks sensitive data, and the API correctly signals
            # is_deleted instead of collapsing an identifiable actor into a
            # generic "Deleted User". ─────────────────────────────────────
            deleteme = User(full_name="AL DeleteMe", email=f"al.deleteme.{suffix}@test.invalid", hashed_password="x", role=TEAM_MEMBER)
            db.add(deleteme)
            await db.commit()
            await db.refresh(deleteme)
            extra_user_ids.append(deleteme.id)

            # 21. actor_label is derived server-side from the real actor —
            # record()'s signature has no caller-suppliable "actor_label"
            # string at all, so a client can never claim actor_label="Admin".
            deleted_actor_probe_label = f"deleted-actor probe {suffix}"
            try:
                await activity_service.record(
                    db, organization_id=org.id, actor=deleteme, action=TASK_UPDATED,
                    entity_type="task", entity_id=999998, entity_label=deleted_actor_probe_label,
                    actor_label="Admin",  # type: ignore[call-arg]
                )
                raise AssertionError("record() must not accept a caller-supplied actor_label")
            except TypeError:
                pass

            await activity_service.record(
                db, organization_id=org.id, actor=deleteme, action=TASK_UPDATED,
                entity_type="task", entity_id=999998, entity_label=deleted_actor_probe_label,
            )
            probe = (await db.execute(select(ActivityLog).where(ActivityLog.entity_label == deleted_actor_probe_label))).scalars().one()
            assert probe.actor_user_id == deleteme.id
            assert probe.actor_label == "AL DeleteMe", "actor_label must be derived from the real actor, not spoofable"
            assert "@" not in probe.actor_label, "actor_label must never leak the actor's email"

            # 21b. A user with NO full_name must never have their email
            # fall back into actor_label — the immutable snapshot only ever
            # stores full_name, or the generic "Unknown User" label, never
            # email/phone/username-as-email/OAuth identity/any login id.
            nameless = User(full_name="", email=f"al.nameless.{suffix}@test.invalid", hashed_password="x", role=TEAM_MEMBER)
            db.add(nameless)
            await db.commit()
            await db.refresh(nameless)
            extra_user_ids.append(nameless.id)
            nameless_probe_label = f"nameless-actor probe {suffix}"
            await activity_service.record(
                db, organization_id=org.id, actor=nameless, action=TASK_UPDATED,
                entity_type="task", entity_id=999996, entity_label=nameless_probe_label,
            )
            nameless_row = (await db.execute(select(ActivityLog).where(ActivityLog.entity_label == nameless_probe_label))).scalars().one()
            assert nameless_row.actor_label == "Unknown User", "a nameless actor must get the generic fallback, never their email"
            assert "@" not in nameless_row.actor_label

            # 22. Renaming the live user afterward must NOT rewrite history —
            # the snapshot is immutable once written.
            deleteme.full_name = "AL Renamed"
            await db.commit()
            # This session uses expire_on_commit=False (app-wide default —
            # see app/core/database.py), so a committed mutation is not
            # automatically reflected in already-loaded ORM objects;
            # populate_existing forces a fresh read of *this* row from the
            # DB to prove the persisted state, not a stale cache — scoped
            # to just this query rather than db.expire_all(), which would
            # also expire unrelated objects (org, other_org, ...) still
            # needed later and require an async context to re-load them.
            reloaded = (await db.execute(
                select(ActivityLog).options(selectinload(ActivityLog.actor))
                .where(ActivityLog.id == probe.id)
                .execution_options(populate_existing=True)
            )).scalar_one()
            assert reloaded.actor_label == "AL DeleteMe", "renaming the live user must never rewrite a past activity_label snapshot"

            # 23. The historical snapshot wins over the live (renamed) name
            # in the API response, even though the live user still exists.
            serialized_before_delete = _serialize(reloaded)
            assert serialized_before_delete.actor.name == "AL DeleteMe", "the historical snapshot must win over the live user's current name"
            assert serialized_before_delete.actor.id == deleteme.id
            assert serialized_before_delete.actor.is_deleted is False

            # 24, 25. Deleting the actor's account (ON DELETE SET NULL) must
            # preserve actor_label — the row survives, only actor_user_id
            # goes NULL — and the API must expose is_deleted=True with the
            # preserved name, never collapsing to an anonymous label.
            await db.execute(delete(User).where(User.id == deleteme.id))
            await db.commit()
            after_delete = (await db.execute(
                select(ActivityLog).options(selectinload(ActivityLog.actor))
                .where(ActivityLog.id == probe.id)
                .execution_options(populate_existing=True)
            )).scalar_one()
            assert after_delete.actor_user_id is None, "sanity check: ON DELETE SET NULL fired"
            assert after_delete.actor_label == "AL DeleteMe", "actor_label must survive the actor's account deletion"
            serialized_after_delete = _serialize(after_delete)
            assert serialized_after_delete.actor.name == "AL DeleteMe", "a recoverable historical actor name must never collapse to a generic 'Deleted User'"
            assert serialized_after_delete.actor.is_deleted is True
            assert serialized_after_delete.actor.id is None
            assert serialized_after_delete.actor.profile_picture_url is None, "no stale avatar for a deleted actor"

            await db.execute(delete(ActivityLog).where(ActivityLog.id == probe.id))
            await db.execute(delete(ActivityLog).where(ActivityLog.id == nameless_row.id))
            await db.commit()

            # 28. Corrective sanitization: rows contaminated by the earlier
            # (pre-privacy-fix) backfill, which used `COALESCE(full_name,
            # email)`, must be cleaned up rather than left carrying an
            # email forever — this is the exact corrective UPDATE shipped
            # in the migration/main.py startup backfill (kept in sync
            # here). Covers both a still-live actor (recovers full_name)
            # and an already-deleted actor (sanitizes to the safe
            # fallback, never fabricating a name).
            legacy_live = User(full_name="AL Legacy Live", email=f"al.legacylive.{suffix}@test.invalid", hashed_password="x", role=TEAM_MEMBER)
            legacy_deleted = User(full_name="AL Legacy Deleted", email=f"al.legacydeleted.{suffix}@test.invalid", hashed_password="x", role=TEAM_MEMBER)
            db.add_all([legacy_live, legacy_deleted])
            await db.commit()
            await db.refresh(legacy_live)
            await db.refresh(legacy_deleted)
            extra_user_ids.append(legacy_live.id)

            legacy_live_row = ActivityLog(
                organization_id=org.id, actor_user_id=legacy_live.id, actor_label=legacy_live.email,
                action=TASK_UPDATED, entity_type="task", entity_id=999995, entity_label="legacy live probe",
            )
            legacy_deleted_email = legacy_deleted.email
            legacy_deleted_row = ActivityLog(
                organization_id=org.id, actor_user_id=None, actor_label=legacy_deleted_email,
                action=TASK_UPDATED, entity_type="task", entity_id=999994, entity_label="legacy deleted probe",
            )
            db.add_all([legacy_live_row, legacy_deleted_row])
            await db.commit()
            await db.execute(delete(User).where(User.id == legacy_deleted.id))
            await db.commit()

            # The same corrective UPDATEs shipped in the migration and
            # main.py's startup backfill.
            await db.execute(text(
                """
                UPDATE activity_logs
                SET actor_label = COALESCE(NULLIF(TRIM(users.full_name), ''), 'Unknown User')
                FROM users
                WHERE activity_logs.actor_user_id = users.id
                  AND activity_logs.actor_label LIKE '%@%'
                """
            ))
            await db.execute(text(
                """
                UPDATE activity_logs
                SET actor_label = 'Unknown User'
                WHERE actor_user_id IS NULL
                  AND actor_label LIKE '%@%'
                """
            ))
            await db.commit()

            fixed_live = (await db.execute(
                select(ActivityLog).where(ActivityLog.id == legacy_live_row.id).execution_options(populate_existing=True)
            )).scalar_one()
            assert fixed_live.actor_label == "AL Legacy Live", "a live actor's legacy email snapshot must be recovered to their full_name"
            assert "@" not in fixed_live.actor_label

            fixed_deleted = (await db.execute(
                select(ActivityLog).where(ActivityLog.id == legacy_deleted_row.id).execution_options(populate_existing=True)
            )).scalar_one()
            assert fixed_deleted.actor_label == "Unknown User", "a deleted actor's legacy email snapshot must be sanitized to the safe fallback, never preserved or fabricated"
            assert legacy_deleted_email not in (fixed_deleted.actor_label or "")

            await db.execute(delete(ActivityLog).where(ActivityLog.id.in_([legacy_live_row.id, legacy_deleted_row.id])))
            await db.commit()

            # 26. Genuinely unrecoverable rows (pre-migration: actor_user_id
            # already NULL, no snapshot ever captured) must not have a name
            # fabricated for them — they fall back to the generic label.
            unrecoverable = ActivityLog(
                organization_id=org.id, actor_user_id=None, actor_label=None,
                action=TASK_UPDATED, entity_type="task", entity_id=999997, entity_label="unrecoverable probe",
            )
            db.add(unrecoverable)
            await db.commit()
            await db.refresh(unrecoverable)
            serialized_unrecoverable = _serialize(unrecoverable)
            assert serialized_unrecoverable.actor.name == "Deleted User"
            assert serialized_unrecoverable.actor.is_deleted is True
            await db.execute(delete(ActivityLog).where(ActivityLog.id == unrecoverable.id))
            await db.commit()

            # ── 20. No PATCH/DELETE route exists for activity logs at all. ────
            import app.api.routes.activity_logs as activity_logs_module
            assert not hasattr(activity_logs_module, "update_activity_log")
            assert not hasattr(activity_logs_module, "delete_activity_log")
            route_paths_and_methods = {
                (route.path, tuple(sorted(route.methods)))
                for route in activity_logs_module.router.routes
            }
            assert not any(m in ("PATCH", "DELETE", "PUT") for _, methods in route_paths_and_methods for m in methods), (
                "ActivityLog must be append-only through the public API — no edit/delete route may exist"
            )

        finally:
            await db.execute(delete(ActivityLog).where(ActivityLog.organization_id.in_([org.id, other_org.id])))
            if created_task_ids:
                await db.execute(delete(Task).where(Task.id.in_(created_task_ids)))
            if created_project_ids:
                await db.execute(delete(Project).where(Project.id.in_(created_project_ids)))
            if created_team_ids:
                await db.execute(delete(Team).where(Team.id.in_(created_team_ids)))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id.in_([org.id, other_org.id])))
            await db.execute(delete(Organization).where(Organization.id.in_([org.id, other_org.id])))
            await db.execute(delete(User).where(User.id.in_([owner.id, member.id, tm.id, pm.id, outsider.id] + extra_user_ids)))
            await db.commit()

    await engine.dispose()


def test_activity_log():
    asyncio.run(_scenario())
