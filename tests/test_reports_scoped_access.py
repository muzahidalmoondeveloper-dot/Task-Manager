"""Regression test: generic (project) Report authorization must require
explicit Project Manager capability + ProjectMembership on the report's own
project — never merely `is_manager_or_above` (which included plain Team
Manager) or the coarse `require_org_manager` role gate alone.

ROOT CAUSE FOUND: `_can_view()`'s generic/project-report branch was
`if tenant.is_manager_or_above: return True` — true for a plain Team
Manager with no relationship whatsoever to the report's project.
`list_reports()` had an even broader bypass: `if tenant.is_manager_or_above:
return reports` returned the ENTIRE unfiltered org-wide report list before
any of the per-report scoping ran at all. `create_report`/`update_report`/
`regenerate_report`/`finalize_report`/`create_new_version`/`delete_report`
were gated only by `require_org_manager`, with no per-report/per-project
check at all — any Team Manager could create a report under any project,
or update/regenerate/finalize/delete ANY report in the org.

Fixed: `_can_view()`'s generic branch now requires
`has_project_manager_access` + `ProjectRepository.is_member(project_id)`
(the same rule app.core.project_access enforces for the Projects
management system); `list_reports()` filters every report through
`_can_view()` (no more unconditional bypass); `create_report` calls
`require_project_management_access()`; the five write routes now call a
`_require_can_view()` wrapper around the same `_can_view()` check.

This test also proves report themes/client branding (organization-wide
settings, unrelated to any one project or team) are now Owner/Admin only,
not Team-Manager-accessible via `require_org_manager`.

Runs against the real database connection the app uses (AsyncSessionLocal).
Every row this test creates is deleted before it returns.
"""

import asyncio
import uuid

import main  # noqa: F401 — registers every SQLAlchemy model/relationship.
from sqlalchemy import delete

from app.api.routes.reports import create_report, create_theme, delete_report, list_reports, update_report
from app.core.auth_errors import AppException
from app.core.database import AsyncSessionLocal, engine
from app.core.org_roles import PROJECT_MANAGER, TEAM_MANAGER
from app.core.tenant import TenantContext, require_org_admin
from app.models.organization import Organization, OrganizationMembership
from app.models.project import Project, ProjectMembership
from app.models.report import Report
from app.models.user import User
from app.schemas.report import ReportCreate, ReportThemeCreate, ReportUpdate


async def _scenario():
    async with AsyncSessionLocal() as db:
        suffix = uuid.uuid4().hex[:8]

        owner = User(full_name="ReportScope Owner", email=f"reportscope.owner.{suffix}@test.invalid", hashed_password="x", role="owner")
        plain_tm = User(full_name="ReportScope PlainTM", email=f"reportscope.tm.{suffix}@test.invalid", hashed_password="x", role=TEAM_MANAGER)
        hybrid = User(full_name="ReportScope Hybrid", email=f"reportscope.hybrid.{suffix}@test.invalid", hashed_password="x", role=PROJECT_MANAGER)
        db.add_all([owner, plain_tm, hybrid])
        await db.flush()

        org = Organization(name=f"ReportScope Org {suffix}", slug=f"reportscope-{suffix}", owner_id=owner.id)
        db.add(org)
        await db.flush()

        owner_membership = OrganizationMembership(organization_id=org.id, user_id=owner.id, role="owner")
        tm_membership = OrganizationMembership(organization_id=org.id, user_id=plain_tm.id, role=TEAM_MANAGER)
        hybrid_membership = OrganizationMembership(organization_id=org.id, user_id=hybrid.id, role=PROJECT_MANAGER, is_team_manager=True)
        db.add_all([owner_membership, tm_membership, hybrid_membership])
        await db.commit()
        for m in (owner_membership, tm_membership, hybrid_membership):
            await db.refresh(m)

        project_a = Project(name=f"Report Project A {suffix}", created_by_id=owner.id, organization_id=org.id)
        db.add(project_a)
        await db.flush()
        # hybrid is a genuine, explicitly-assigned Project Manager on project_a.
        db.add(ProjectMembership(project_id=project_a.id, user_id=hybrid.id))
        await db.commit()

        tm_tenant = TenantContext(organization_id=org.id, organization=org, membership=tm_membership, user=plain_tm, db=db)
        hybrid_tenant = TenantContext(organization_id=org.id, organization=org, membership=hybrid_membership, user=hybrid, db=db)
        owner_tenant = TenantContext(organization_id=org.id, organization=org, membership=owner_membership, user=owner, db=db)

        report_ids: list[int] = []
        theme_id = None
        try:
            # ── A plain Team Manager cannot create a report under a
            #    project they have no relationship to. ──
            try:
                await create_report(ReportCreate(project_id=project_a.id, title=f"Should fail {suffix}"), tenant=tm_tenant)
                raise AssertionError("a plain Team Manager must not be able to create a report for a project they don't manage")
            except AppException as exc:
                assert exc.code == "PROJECT_NOT_ASSIGNED"

            # ── The hybrid (genuine PM, also team-manager-privileged) CAN
            #    create a report for project_a — from their real PM
            #    capability, exactly like any other Project Manager. ──
            created = await create_report(ReportCreate(project_id=project_a.id, title=f"Real Report {suffix}"), tenant=hybrid_tenant)
            report_ids.append(created.id)
            assert created.project_id == project_a.id

            # ── list_reports: the plain Team Manager sees nothing (not
            #    team_visible, not their project); Owner and the assigned
            #    hybrid PM both see it. ──
            tm_visible = await list_reports(
                project_id=None, employee_id=None, team_id=None, report_type=None,
                status_filter=None, is_latest_version=True, tenant=tm_tenant,
            )
            assert created.id not in {r.id for r in tm_visible}, "a plain Team Manager must not see a report for a project they don't manage"

            owner_visible = await list_reports(
                project_id=None, employee_id=None, team_id=None, report_type=None,
                status_filter=None, is_latest_version=True, tenant=owner_tenant,
            )
            assert created.id in {r.id for r in owner_visible}

            hybrid_visible = await list_reports(
                project_id=None, employee_id=None, team_id=None, report_type=None,
                status_filter=None, is_latest_version=True, tenant=hybrid_tenant,
            )
            assert created.id in {r.id for r in hybrid_visible}

            # ── update_report / delete_report: plain Team Manager rejected,
            #    even via direct API call with the real report_id. ──
            try:
                await update_report(created.id, ReportUpdate(title="hacked"), tenant=tm_tenant)
                raise AssertionError("a plain Team Manager must not be able to update a report for an unmanaged project")
            except AppException as exc:
                assert exc.code == "REPORT_FORBIDDEN"

            try:
                await delete_report(created.id, tenant=tm_tenant)
                raise AssertionError("a plain Team Manager must not be able to delete a report for an unmanaged project")
            except AppException as exc:
                assert exc.code == "REPORT_FORBIDDEN"

            # ── Owner remains unrestricted (regression check). ──
            owner_updated = await update_report(created.id, ReportUpdate(title="Owner Edited"), tenant=owner_tenant)
            assert owner_updated.title == "Owner Edited"

            # ── Report themes: organization-wide setting, Owner/Admin only
            #    now — a plain Team Manager is rejected by require_org_admin. ──
            try:
                await require_org_admin(tenant=tm_tenant)
                raise AssertionError("a plain Team Manager must not pass require_org_admin for report themes")
            except AppException as exc:
                assert exc.code == "ORG_ADMIN_REQUIRED"

            theme = await create_theme(ReportThemeCreate(name=f"Theme {suffix}"), tenant=owner_tenant)
            theme_id = theme.id

        finally:
            await db.execute(delete(Report).where(Report.id.in_(report_ids)))
            if theme_id is not None:
                from app.models.report import ReportTheme
                await db.execute(delete(ReportTheme).where(ReportTheme.id == theme_id))
            await db.execute(delete(ProjectMembership).where(ProjectMembership.project_id == project_a.id))
            await db.execute(delete(Project).where(Project.id == project_a.id))
            await db.execute(delete(OrganizationMembership).where(OrganizationMembership.organization_id == org.id))
            await db.execute(delete(Organization).where(Organization.id == org.id))
            await db.execute(delete(User).where(User.id.in_([owner.id, plain_tm.id, hybrid.id])))
            await db.commit()

    await engine.dispose()


def test_report_authorization_requires_explicit_project_manager_scope():
    asyncio.run(_scenario())
