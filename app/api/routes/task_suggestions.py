import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.tenant import TenantContext, enforce_feature, require_org_admin
from app.services.automation_tasks import analyze_pending_sources_for_user

router = APIRouter(prefix="/task-suggestions", tags=["Task Automation"])
logger = logging.getLogger("task_suggestions")


@router.post("/sync-yesterday")
async def sync_yesterday_sources(
    tenant: TenantContext = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db),
):
    """Analyze whatever emails/meeting transcripts are already imported and
    pending analysis for this user, and create tasks directly.

    Kept as its own admin-only entry point (unlike the provider-specific
    manual sync routes, this one skips the fetch stage entirely and just
    re-runs analysis over whatever is already sitting in
    processing_status="discovered") — renamed from "sync yesterday" since
    analyze_pending_sources_for_user() no longer operates on a fixed
    yesterday window (see that function's own docstring); the endpoint
    path is left unchanged to avoid an unrelated frontend/API contract
    break."""
    enforce_feature(tenant, "has_ai_features")
    logger.info("Manual analysis-only trigger", extra={"user_id": tenant.user.id, "organization_id": str(tenant.organization_id)})
    result = await analyze_pending_sources_for_user(db=db, user=tenant.user, org_id=tenant.organization_id)

    return {
        "triggered_at": datetime.now(timezone.utc).isoformat(),
        **result,
        "message": (
            f"Analyzed {result.get('sources_analyzed', 0)} sources. "
            f"Created {result.get('tasks_created', 0)} tasks automatically."
        ),
    }
