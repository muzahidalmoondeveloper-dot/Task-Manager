from fastapi import APIRouter, Depends, Query
from fastapi import status as http_status

from app.core.auth_errors import AppException, ErrorDef
from app.core.team_access import require_team_access
from app.core.tenant import TenantContext, get_tenant_context, require_org_manager
from app.repositories.risk_repository import RiskRepository
from app.repositories.team_repository import TeamRepository
from app.schemas.risk import RiskCreate, RiskOut, RiskUpdate

router = APIRouter(prefix="/teams/{team_id}/risks", tags=["Risks"])

_RISK_NOT_FOUND = ErrorDef(code="RISK_NOT_FOUND", status=http_status.HTTP_404_NOT_FOUND, message="Risk not found.")


# Risks are team-scoped (Risk.team_id, not org-wide or project-wide — the
# optional Risk.project_id is a contextual label only, same as a KPI's
# optional project_id, and never itself grants project access). Every
# route below now also calls require_team_access(team_id) — previously
# only require_org_manager gated writes here (Owner/Admin/Team Manager),
# with NO check that the caller actually manages/belongs to *this specific*
# team_id from the URL, unlike the identical-shape kpi.py routes (which
# already call the same helper via their own _require_team wrapper). That
# gap let any Team Manager create/update/delete Risks for an arbitrary
# team_id, not just one they manage — matches app.core.team_access's rule
# exactly (Owner/Admin unrestricted; everyone else scoped to a team they
# manage or are a member of).
async def _require_team(tenant: TenantContext, team_id: int) -> None:
    await require_team_access(tenant, TeamRepository(tenant.db, tenant.organization_id), team_id)


@router.get("", response_model=list[RiskOut])
async def list_risks(
    team_id: int,
    status_filter: str | None = Query(default=None, alias="status"),
    project_id: int | None = Query(default=None),
    tenant: TenantContext = Depends(get_tenant_context),
):
    await _require_team(tenant, team_id)
    repo = RiskRepository(tenant.db, tenant.organization_id)
    return await repo.list_by_team(team_id, status=status_filter, project_id=project_id)


@router.post("", response_model=RiskOut, status_code=http_status.HTTP_201_CREATED)
async def create_risk(
    team_id: int,
    payload: RiskCreate,
    tenant: TenantContext = Depends(require_org_manager),
):
    await _require_team(tenant, team_id)
    repo = RiskRepository(tenant.db, tenant.organization_id)
    return await repo.create(team_id, payload)


@router.patch("/{risk_id}", response_model=RiskOut)
async def update_risk(
    team_id: int,
    risk_id: int,
    payload: RiskUpdate,
    tenant: TenantContext = Depends(require_org_manager),
):
    await _require_team(tenant, team_id)
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
    await _require_team(tenant, team_id)
    repo = RiskRepository(tenant.db, tenant.organization_id)
    risk = await repo.get_by_id(risk_id)
    if risk is None or risk.team_id != team_id:
        raise AppException(_RISK_NOT_FOUND)
    await repo.delete(risk)
    return None
