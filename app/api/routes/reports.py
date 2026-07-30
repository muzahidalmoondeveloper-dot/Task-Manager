from datetime import UTC, date, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from fastapi import status as http_status
from fastapi.responses import Response

from app.api.routes.scoreboard import _require_can_view_scoreboard
from app.api.routes.team_scoreboard import _get_team_or_404, _require_can_view_team_scoreboard
from app.core.auth_errors import AppException, ErrorDef
from app.core.org_roles import CLIENT
from app.core.tenant import TenantContext, get_tenant_context, require_org_manager
from app.models.report import Report, ReportContent
from app.repositories.project_repository import ProjectRepository
from app.repositories.report_repository import ReportRepository
from app.repositories.user_repository import UserRepository
from app.schemas.report import (
    ClientBrandingOut,
    ClientBrandingUpsert,
    EmployeeReportCreate,
    ReportCreate,
    ReportDetail,
    ReportListItem,
    ReportThemeCreate,
    ReportThemeOut,
    ReportThemeUpdate,
    ReportUpdate,
    TeamReportCreate,
)
from app.services import performance_pdf_service
from app.services.pdf_render_service import PdfRenderService
from app.services.report_generation_service import ReportGenerationService
from app.services.report_snapshot_service import ReportSnapshotService
from app.services.report_theme_service import ReportThemeService

router = APIRouter(prefix="/reports", tags=["Reports"])

_REPORT_NOT_FOUND = ErrorDef(code="REPORT_NOT_FOUND", status=http_status.HTTP_404_NOT_FOUND, message="Report not found.")
_PROJECT_INVALID = ErrorDef(code="PROJECT_INVALID", status=http_status.HTTP_400_BAD_REQUEST, message="Selected project is invalid.")
_REPORT_FINALIZED = ErrorDef(code="REPORT_FINALIZED", status=http_status.HTTP_409_CONFLICT, message="Finalized reports cannot be edited. Create a new version instead.")
_USER_NOT_FOUND = ErrorDef(code="USER_NOT_FOUND", status=http_status.HTTP_404_NOT_FOUND, message="User not found.")

_PERFORMANCE_REPORT_TYPES = {"employee_performance", "team_performance"}


async def _get_report_or_404(tenant: TenantContext, report_id: int):
    repo = ReportRepository(tenant.db, tenant.organization_id)
    report = await repo.get_by_id(report_id)
    if report is None:
        raise AppException(_REPORT_NOT_FOUND)
    return report


def _is_client_safe(report) -> bool:
    """A Client may only ever see finalized, latest-version, client-type reports."""
    return report.report_type == "client" and report.status == "finalized" and report.is_latest_version


async def _can_view(tenant: TenantContext, report) -> bool:
    if report.report_type == "employee_performance":
        try:
            await _require_can_view_scoreboard(tenant, report.employee_id)
            return True
        except AppException:
            return False

    if report.report_type == "team_performance":
        team = await _get_team_or_404(tenant, report.team_id)
        try:
            await _require_can_view_team_scoreboard(tenant, team)
            return True
        except AppException:
            return False

    if tenant.is_manager_or_above:
        return True
    if tenant.org_role == CLIENT:
        if not _is_client_safe(report):
            return False
        project_repo = ProjectRepository(tenant.db, tenant.organization_id)
        return await project_repo.is_member(report.project_id, tenant.user.id)
    return report.team_visible


@router.get("", response_model=list[ReportListItem])
async def list_reports(
    project_id: int | None = Query(default=None),
    employee_id: int | None = Query(default=None),
    team_id: int | None = Query(default=None),
    report_type: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    is_latest_version: bool = Query(default=True),
    tenant: TenantContext = Depends(get_tenant_context),
):
    repo = ReportRepository(tenant.db, tenant.organization_id)
    reports = await repo.list_all(
        project_id=project_id, report_type=report_type, status=status_filter, latest_only=is_latest_version,
    )
    if employee_id is not None:
        reports = [r for r in reports if r.employee_id == employee_id]
    if team_id is not None:
        reports = [r for r in reports if r.team_id == team_id]

    if tenant.is_manager_or_above:
        return reports

    visible = []
    for r in reports:
        if r.report_type in _PERFORMANCE_REPORT_TYPES:
            if await _can_view(tenant, r):
                visible.append(r)
        elif tenant.org_role == CLIENT:
            project_repo = ProjectRepository(tenant.db, tenant.organization_id)
            if _is_client_safe(r) and await project_repo.is_member(r.project_id, tenant.user.id):
                visible.append(r)
        elif r.team_visible:
            visible.append(r)
    return visible


@router.post("", response_model=ReportDetail, status_code=http_status.HTTP_201_CREATED)
async def create_report(
    payload: ReportCreate,
    tenant: TenantContext = Depends(require_org_manager),
):
    project_repo = ProjectRepository(tenant.db, tenant.organization_id)
    if await project_repo.get_by_id(payload.project_id) is None:
        raise AppException(_PROJECT_INVALID)

    repo = ReportRepository(tenant.db, tenant.organization_id)
    report = await repo.create(payload, created_by_id=tenant.user.id)

    generation = ReportGenerationService(tenant.db, tenant.organization_id)
    await generation.generate(report)
    return await repo.get_by_id(report.id)


@router.post("/employee", response_model=ReportDetail, status_code=http_status.HTTP_201_CREATED)
async def create_employee_report(
    payload: EmployeeReportCreate,
    tenant: TenantContext = Depends(get_tenant_context),
):
    await _require_can_view_scoreboard(tenant, payload.employee_id)

    employee = await UserRepository(tenant.db).get_by_id(payload.employee_id)
    if employee is None:
        raise AppException(_USER_NOT_FOUND)

    try:
        report = Report(
            organization_id=tenant.organization_id,
            report_type="employee_performance",
            title=f"Employee Performance Report — {employee.full_name}",
            employee_id=employee.id,
            status="draft",
            created_by_id=tenant.user.id,
        )
        tenant.db.add(report)
        await tenant.db.flush()

        pdf_bytes, snapshot = await performance_pdf_service.generate_employee_pdf(
            tenant.db, tenant.organization, employee, tenant.user.full_name, report,
            period=payload.period, project_id=payload.project_id, team_id=payload.team_id,
            start_date=payload.start_date, end_date=payload.end_date,
            include_task_details=payload.include_task_details,
        )
    except ValueError as exc:
        raise AppException(ErrorDef(code="SCOREBOARD_INVALID_PERIOD", status=http_status.HTTP_400_BAD_REQUEST, message=str(exc)))

    await _finalize_performance_report(tenant, report, pdf_bytes, snapshot, "employee", employee.full_name)

    repo = ReportRepository(tenant.db, tenant.organization_id)
    return await repo.get_by_id(report.id)


@router.post("/team", response_model=ReportDetail, status_code=http_status.HTTP_201_CREATED)
async def create_team_report(
    payload: TeamReportCreate,
    tenant: TenantContext = Depends(get_tenant_context),
):
    team = await _get_team_or_404(tenant, payload.team_id)
    await _require_can_view_team_scoreboard(tenant, team)

    try:
        report = Report(
            organization_id=tenant.organization_id,
            report_type="team_performance",
            title=f"Team Performance Report — {team.name}",
            team_id=team.id,
            status="draft",
            created_by_id=tenant.user.id,
        )
        tenant.db.add(report)
        await tenant.db.flush()

        pdf_bytes, snapshot = await performance_pdf_service.generate_team_pdf(
            tenant.db, tenant.organization, team, tenant.user.full_name, report,
            period=payload.period, project_id=payload.project_id,
            start_date=payload.start_date, end_date=payload.end_date,
            include_task_details=payload.include_task_details,
        )
    except ValueError as exc:
        raise AppException(ErrorDef(code="SCOREBOARD_INVALID_PERIOD", status=http_status.HTTP_400_BAD_REQUEST, message=str(exc)))

    await _finalize_performance_report(tenant, report, pdf_bytes, snapshot, "team", team.name)

    repo = ReportRepository(tenant.db, tenant.organization_id)
    return await repo.get_by_id(report.id)


async def _finalize_performance_report(tenant, report, pdf_bytes: bytes, snapshot: dict, kind: str, subject_name: str) -> None:
    report.period_start = date.fromisoformat(snapshot["period_start"])
    report.period_end = date.fromisoformat(snapshot["period_end"])
    report.performance_snapshot = snapshot

    pdf_service = PdfRenderService()
    filename = performance_pdf_service.build_performance_filename(kind, subject_name, report.period_start, report.period_end)
    path = pdf_service.output_path(report, filename)
    path.write_bytes(pdf_bytes)

    report.pdf_file_path = str(path)
    report.status = "finalized"
    report.finalized_by_id = tenant.user.id
    report.finalized_at = datetime.now(UTC)
    await tenant.db.commit()


async def _regenerate_performance_pdf(tenant: TenantContext, report) -> tuple[bytes, str]:
    """Re-renders a performance report's PDF from its stored snapshot filters
    — used only if the cached file on disk has gone missing."""
    snapshot = report.performance_snapshot or {}
    project_id = snapshot.get("project_id")
    include_task_details = snapshot.get("include_task_details", False)

    if report.report_type == "employee_performance":
        employee = await UserRepository(tenant.db).get_by_id(report.employee_id)
        pdf_bytes, new_snapshot = await performance_pdf_service.generate_employee_pdf(
            tenant.db, tenant.organization, employee, tenant.user.full_name, report,
            period="custom", project_id=project_id, team_id=snapshot.get("team_id"),
            start_date=report.period_start, end_date=report.period_end,
            include_task_details=include_task_details,
        )
        subject_name = employee.full_name
        kind = "employee"
    else:
        team = await _get_team_or_404(tenant, report.team_id)
        pdf_bytes, new_snapshot = await performance_pdf_service.generate_team_pdf(
            tenant.db, tenant.organization, team, tenant.user.full_name, report,
            period="custom", project_id=project_id,
            start_date=report.period_start, end_date=report.period_end,
            include_task_details=include_task_details,
        )
        subject_name = team.name
        kind = "team"

    report.performance_snapshot = new_snapshot
    filename = performance_pdf_service.build_performance_filename(kind, subject_name, report.period_start, report.period_end)
    return pdf_bytes, filename


_THEME_NOT_FOUND = ErrorDef(code="REPORT_THEME_NOT_FOUND", status=http_status.HTTP_404_NOT_FOUND, message="Theme not found.")


# ── Themes & branding (static paths — must be registered before /{report_id}) ──

@router.get("/themes", response_model=list[ReportThemeOut])
async def list_themes(tenant: TenantContext = Depends(get_tenant_context)):
    service = ReportThemeService(tenant.db, tenant.organization_id)
    return await service.list_themes_with_presets()


@router.post("/themes", response_model=ReportThemeOut, status_code=http_status.HTTP_201_CREATED)
async def create_theme(payload: ReportThemeCreate, tenant: TenantContext = Depends(require_org_manager)):
    repo = ReportRepository(tenant.db, tenant.organization_id)
    return await repo.create_theme(payload)


@router.patch("/themes/{theme_id}", response_model=ReportThemeOut)
async def update_theme(theme_id: int, payload: ReportThemeUpdate, tenant: TenantContext = Depends(require_org_manager)):
    repo = ReportRepository(tenant.db, tenant.organization_id)
    theme = await repo.get_theme(theme_id)
    if theme is None:
        raise AppException(_THEME_NOT_FOUND)
    return await repo.update_theme(theme, payload)


@router.get("/branding", response_model=list[ClientBrandingOut])
async def list_branding(tenant: TenantContext = Depends(get_tenant_context)):
    repo = ReportRepository(tenant.db, tenant.organization_id)
    return await repo.list_branding()


@router.post("/branding", response_model=ClientBrandingOut, status_code=http_status.HTTP_201_CREATED)
async def upsert_branding(payload: ClientBrandingUpsert, tenant: TenantContext = Depends(require_org_manager)):
    repo = ReportRepository(tenant.db, tenant.organization_id)
    return await repo.upsert_branding(payload)


@router.get("/{report_id}", response_model=ReportDetail)
async def get_report(report_id: int, tenant: TenantContext = Depends(get_tenant_context)):
    report = await _get_report_or_404(tenant, report_id)
    if not await _can_view(tenant, report):
        raise AppException(ErrorDef(code="REPORT_FORBIDDEN", status=http_status.HTTP_403_FORBIDDEN, message="You do not have access to this report."))
    return report


@router.patch("/{report_id}", response_model=ReportDetail)
async def update_report(
    report_id: int,
    payload: ReportUpdate,
    tenant: TenantContext = Depends(require_org_manager),
):
    report = await _get_report_or_404(tenant, report_id)
    if report.status == "finalized":
        raise AppException(_REPORT_FINALIZED)

    data = payload.model_dump(exclude_unset=True, exclude={"content"})
    for key, value in data.items():
        setattr(report, key, value)

    if payload.content is not None:
        if report.content is None:
            report.content = ReportContent(report_id=report.id)
            tenant.db.add(report.content)
        content_data = payload.content.model_dump(exclude_unset=True)
        for key, value in content_data.items():
            setattr(report.content, key, value)

    await tenant.db.commit()
    repo = ReportRepository(tenant.db, tenant.organization_id)
    return await repo.get_by_id(report_id)


@router.post("/{report_id}/regenerate", response_model=ReportDetail)
async def regenerate_report(report_id: int, tenant: TenantContext = Depends(require_org_manager)):
    report = await _get_report_or_404(tenant, report_id)
    if report.status == "finalized":
        raise AppException(_REPORT_FINALIZED)

    generation = ReportGenerationService(tenant.db, tenant.organization_id)
    await generation.generate(report)

    repo = ReportRepository(tenant.db, tenant.organization_id)
    return await repo.get_by_id(report_id)


async def _serve_pdf(tenant: TenantContext, report_id: int, *, as_attachment: bool) -> Response:
    report = await _get_report_or_404(tenant, report_id)
    if not await _can_view(tenant, report):
        raise AppException(ErrorDef(code="REPORT_FORBIDDEN", status=http_status.HTTP_403_FORBIDDEN, message="You do not have access to this report."))

    pdf_service = PdfRenderService()

    pdf_bytes: bytes
    filename: str
    stored_path = Path(report.pdf_file_path) if report.pdf_file_path else None
    if stored_path is not None and stored_path.exists() and report.status == "finalized":
        pdf_bytes = stored_path.read_bytes()
        filename = stored_path.name
    elif report.report_type in _PERFORMANCE_REPORT_TYPES:
        # The cached file has gone missing — rebuild from the stored snapshot
        # filters via the shared Scoreboard calculation service, never the
        # Rock/KPI-shaped project-report renderer.
        pdf_bytes, filename = await _regenerate_performance_pdf(tenant, report)
        path = pdf_service.output_path(report, filename)
        path.write_bytes(pdf_bytes)
        report.pdf_file_path = str(path)
        await tenant.db.commit()
    else:
        pdf_bytes, filename = await pdf_service.render_pdf(report)
        if report.status != "finalized":
            # Cache the rendered draft so repeat previews don't re-render.
            path = pdf_service.output_path(report, filename)
            path.write_bytes(pdf_bytes)
            report.pdf_file_path = str(path)
            await tenant.db.commit()

    disposition = "attachment" if as_attachment else "inline"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'{disposition}; filename="{filename}"'},
    )


@router.get("/{report_id}/preview")
async def preview_report(report_id: int, tenant: TenantContext = Depends(get_tenant_context)):
    return await _serve_pdf(tenant, report_id, as_attachment=False)


@router.get("/{report_id}/download")
async def download_report(report_id: int, tenant: TenantContext = Depends(get_tenant_context)):
    return await _serve_pdf(tenant, report_id, as_attachment=True)


@router.post("/{report_id}/finalize", response_model=ReportDetail)
async def finalize_report(report_id: int, tenant: TenantContext = Depends(require_org_manager)):
    report = await _get_report_or_404(tenant, report_id)
    if report.status == "finalized":
        raise AppException(ErrorDef(code="REPORT_ALREADY_FINALIZED", status=http_status.HTTP_409_CONFLICT, message="This report is already finalized."))

    pdf_service = PdfRenderService()
    stored_path = await pdf_service.render_and_store(report)

    report.status = "finalized"
    report.finalized_by_id = tenant.user.id
    report.finalized_at = datetime.now(UTC)
    report.pdf_file_path = stored_path
    await tenant.db.commit()

    repo = ReportRepository(tenant.db, tenant.organization_id)
    return await repo.get_by_id(report_id)


@router.post("/{report_id}/new-version", response_model=ReportDetail, status_code=http_status.HTTP_201_CREATED)
async def create_new_version(report_id: int, tenant: TenantContext = Depends(require_org_manager)):
    report = await _get_report_or_404(tenant, report_id)
    if report.status != "finalized":
        raise AppException(ErrorDef(code="REPORT_NOT_FINALIZED", status=http_status.HTTP_400_BAD_REQUEST, message="Only finalized reports can be turned into a new version."))

    snapshot_service = ReportSnapshotService(tenant.db, tenant.organization_id)
    new_report = await snapshot_service.duplicate_as_new_version(report, created_by_id=tenant.user.id)

    repo = ReportRepository(tenant.db, tenant.organization_id)
    return await repo.get_by_id(new_report.id)


@router.get("/{report_id}/versions", response_model=list[ReportListItem])
async def list_versions(report_id: int, tenant: TenantContext = Depends(get_tenant_context)):
    report = await _get_report_or_404(tenant, report_id)
    if not await _can_view(tenant, report):
        raise AppException(ErrorDef(code="REPORT_FORBIDDEN", status=http_status.HTTP_403_FORBIDDEN, message="You do not have access to this report."))

    root_id = report.parent_report_id or report.id
    repo = ReportRepository(tenant.db, tenant.organization_id)
    version_rows = await repo.list_versions(root_id)
    report_ids = {root_id, *(v.report_id for v in version_rows)}
    return [r for r in [await repo.get_by_id(rid) for rid in report_ids] if r is not None]


@router.delete("/{report_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_report(report_id: int, tenant: TenantContext = Depends(require_org_manager)):
    report = await _get_report_or_404(tenant, report_id)
    if report.status == "finalized":
        raise AppException(_REPORT_FINALIZED, message="Finalized reports cannot be deleted; archive them instead.")
    repo = ReportRepository(tenant.db, tenant.organization_id)
    await repo.delete(report)
    return None
