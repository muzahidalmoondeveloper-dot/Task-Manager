import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.tenant import TenantContext, enforce_feature, require_org_admin
from app.services.automation_tasks import analyze_yesterday_sources_for_user

router = APIRouter(prefix="/task-suggestions", tags=["Task Automation"])
logger = logging.getLogger("task_suggestions")


@router.post("/sync-yesterday")
async def sync_yesterday_sources(
    tenant: TenantContext = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db),
):
    """Analyse yesterday's emails and meeting transcripts and create tasks directly."""
    enforce_feature(tenant, "has_ai_features")
    logger.info("Manual sync triggered by user: id=%s email=%s", tenant.user.id, tenant.user.email)
    result = await analyze_yesterday_sources_for_user(db=db, user=tenant.user, org_id=tenant.organization_id)

    return {
        "period": "yesterday",
        "triggered_at": datetime.now(timezone.utc).isoformat(),
        **result,
        "message": (
            f"Analysed {result.get('sources_analyzed', 0)} sources. "
            f"Created {result.get('tasks_created', 0)} tasks automatically."
        ),
    }
