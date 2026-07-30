from fastapi import APIRouter, Depends, Query
from fastapi import status as http_status

from app.core.auth_errors import AppException, ErrorDef
from app.core.tenant import TenantContext, get_tenant_context, require_org_manager
from app.repositories.risk_repository import RiskRepository
from app.schemas.risk import RiskCreate, RiskOut, RiskUpdate

router = APIRouter(prefix="/teams/{team_id}/risks", tags=["Risks"])

_RISK_NOT_FOUND = ErrorDef(code="RISK_NOT_FOUND", status=http_status.HTTP_404_NOT_FOUND, message="Risk not found.")


@router.get("", response_model=list[RiskOut])
async def list_risks(
    team_id: int,
    status_filter: str | None = Query(default=None, alias="status"),
    project_id: int | None = Query(default=None),
    tenant: TenantContext = Depends(get_tenant_context),
):
    repo = RiskRepository(tenant.db, tenant.organization_id)
    return await repo.list_by_team(team_id, status=status_filter, project_id=project_id)


@router.post("", response_model=RiskOut, status_code=http_status.HTTP_201_CREATED)
async def create_risk(
    team_id: int,
    payload: RiskCreate,
    tenant: TenantContext = Depends(require_org_manager),
):
    repo = RiskRepository(tenant.db, tenant.organization_id)
    return await repo.create(team_id, payload)


@router.patch("/{risk_id}", response_model=RiskOut)
async def update_risk(
    team_id: int,
    risk_id: int,
    payload: RiskUpdate,
    tenant: TenantContext = Depends(require_org_manager),
):
    repo = RiskRepository(tenant.db, tenant.organization_id)
    risk = await repo.get_by_id(risk_id)
    if risk is None or risk.team_id != team_id:
        raise AppException(_RISK_NOT_FOUND)
    return await repo.update(risk, payload)


@router.delete("/{risk_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_risk(
    team_id: int,
    risk_id: int,
    tenant: TenantContext = Depends(require_org_manager),
):
    repo = RiskRepository(tenant.db, tenant.organization_id)
    risk = await repo.get_by_id(risk_id)
    if risk is None or risk.team_id != team_id:
        raise AppException(_RISK_NOT_FOUND)
    await repo.delete(risk)
    return None
