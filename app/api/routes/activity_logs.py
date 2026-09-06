from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.tenant import TenantContext, require_org_admin
from app.models.activity_log import ActivityLog
from app.repositories.activity_log_repository import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    ActivityLogRepository,
)
from app.schemas.activity_log import ActivityActorSummary, ActivityLogPage, ActivityLogRead

router = APIRouter(prefix="/activity-logs", tags=["Activity Log"])


def _serialize(entry: ActivityLog) -> ActivityLogRead:
    actor = entry.actor  # already selectinload'ed — no N+1 per row

    # Name precedence (Task #8 follow-up — PRESERVE DELETED ACTOR IDENTITY):
    # the immutable `actor_label` snapshot always wins over the live user's
    # *current* name, even when that user still exists — this is an audit
    # log, so it must read as "who did this at the time", not be silently
    # rewritten every time someone renames themselves. `actor_label` is
    # only ever absent for rows written before this column existed and
    # whose backfill couldn't recover a name (see the migration); for
    # those, and only those, we fall back to the live user's current name,
    # and finally to a generic label when no identity survives at all.
    if entry.actor_label:
        name = entry.actor_label
    elif actor is not None:
        name = actor.full_name or actor.email or "Unknown User"
    else:
        name = "Deleted User"

    # id / profile_picture_url intentionally come from the *live* relation
    # only — never snapshotted (a stale avatar for a deleted account would
    # be misleading, and the id of a deleted user is meaningless to link
    # to). Both are None exactly when the account has been deleted;
    # `is_deleted` makes that explicit instead of leaving the frontend to
    # infer it from `id` being absent.
    actor_summary = ActivityActorSummary(
        id=actor.id if actor is not None else None,
        name=name,
        profile_picture_url=getattr(actor, "profile_picture_url", None) if actor is not None else None,
        is_deleted=actor is None,
    )

    return ActivityLogRead(
        id=entry.id,
        action=entry.action,
        actor=actor_summary,
        entity_type=entry.entity_type,
        entity_id=entry.entity_id,
        entity_label=entry.entity_label,
        metadata=entry.activity_metadata,
        created_at=entry.created_at,
    )


@router.get("", response_model=ActivityLogPage)
async def list_activity_logs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    actor_user_id: int | None = None,
    action: str | None = None,
    entity_type: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    tenant: TenantContext = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db),
):
    """Organization-wide User Activity Log (Task #8). Owner/Admin only —
    the exact same `require_org_admin` gate that already protects the
    org-wide Users system (GET /users), so a plain Team Manager or Project
    Manager gets the identical 403 they already get there; no new,
    possibly-inconsistent authorization rule was introduced for this
    endpoint. Newest-first, paginated (bounded page size), scoped strictly
    to the caller's own organization via ActivityLogRepository(db,
    tenant.organization_id) — org_id is never taken from the request.
    """
    repo = ActivityLogRepository(db, tenant.organization_id)
    items, total = await repo.list_page(
        page=page, page_size=page_size, actor_user_id=actor_user_id,
        action=action, entity_type=entity_type, since=since, until=until,
    )
    return ActivityLogPage(
        items=[_serialize(entry) for entry in items],
        page=page,
        page_size=page_size,
        total=total,
    )
