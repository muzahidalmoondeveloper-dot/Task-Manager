"""Renders a Report to PDF using Jinja2 (HTML/CSS) + Playwright (Chromium).

The exact same HTML/CSS this module produces is used for both the in-app
preview and the downloaded file (spec §26/§35) — there is only one render
path, `render_html`, and one `render_pdf` on top of it.
"""

from __future__ import annotations

import asyncio
import re
import sys
import unicodedata
from datetime import date, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.core.config import get_settings
from app.models.report import Report

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "reports"
_PRINT_CSS_PATH = _TEMPLATE_DIR / "report_print.css"

_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),
)


def _slugify(value: str | None) -> str:
    if not value:
        return "untitled"
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^\w\s-]", "", value).strip().lower()
    value = re.sub(r"[-\s]+", "-", value)
    return value[:80] or "untitled"


def _render_pdf_sync(html: str) -> bytes:
    """Runs on a worker thread — see ``render_pdf`` for why sync, not async.

    Playwright's sync API still creates its own asyncio event loop internally
    via ``asyncio.new_event_loop()``, which goes through the process-wide
    event loop *policy* — not per-thread. uvicorn runs a SelectorEventLoop on
    Windows (no subprocess support), so without this override Playwright's
    internal loop would inherit that and fail the same way. Switching the
    policy here only affects loops created after this point; it does not
    touch uvicorn's already-running main-thread loop.
    """
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content(html, wait_until="networkidle")
            return page.pdf(format="A4", print_background=True)
        finally:
            browser.close()


def build_filename(report: Report) -> str:
    project_slug = _slugify(report.project.name if report.project else "project")
    client_slug = _slugify(report.branding.client_name if report.branding else "internal")
    type_slug = _slugify(report.report_type)
    period_slug = _slugify(str(report.period_start) if report.period_start else "period")
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"{project_slug}_{client_slug}_{type_slug}-report_{period_slug}_v{report.version}_{timestamp}.pdf"


class PdfRenderService:
    def __init__(self) -> None:
        self.settings = get_settings()

    async def render_html(self, report: Report) -> str:
        from app.services.report_calculation_service import dashboard_summary

        calculated = dashboard_summary(report)

        template = _env.get_template("report_base.html")
        return template.render(
            report=report,
            content=report.content,
            theme=report.theme,
            branding=report.branding,
            rocks=report.rock_snapshots,
            kpis=report.kpi_snapshots,
            milestones=report.milestone_snapshots,
            tasks=report.task_snapshots,
            risks=report.risk_snapshots,
            issues=report.issue_snapshots,
            client_actions=report.client_actions,
            calculated=calculated,
            generated_date=date.today().isoformat(),
            print_css=_PRINT_CSS_PATH.read_text(encoding="utf-8"),
        )

    async def render_html_string(self, html: str) -> bytes:
        """Renders an arbitrary, already-built HTML string to PDF bytes —
        reused by report generators that don't build off a `Report` ORM
        object (e.g. Employee/Team Performance reports). Goes through the
        exact same Windows-safe worker-thread path as `render_pdf` below."""
        return await asyncio.to_thread(_render_pdf_sync, html)

    async def render_pdf(self, report: Report) -> tuple[bytes, str]:
        """Returns (pdf_bytes, filename). Requires `playwright install chromium`
        to have been run once in this environment.

        Uses Playwright's *sync* API inside a worker thread rather than the
        async API on the main loop: on Windows, uvicorn's default
        SelectorEventLoop cannot spawn subprocesses (asyncio limitation), which
        the async API requires. The sync API launches Chromium via a plain
        blocking subprocess in its own thread, sidestepping that limitation.
        """
        html = await self.render_html(report)
        pdf_bytes = await asyncio.to_thread(_render_pdf_sync, html)
        return pdf_bytes, build_filename(report)

    def output_path(self, report: Report, filename: str) -> Path:
        base = Path(self.settings.PDF_OUTPUT_DIR)
        org_dir = base / str(report.organization_id) / str(report.project_id or "no-project")
        org_dir.mkdir(parents=True, exist_ok=True)
        return org_dir / filename

    async def render_and_store(self, report: Report) -> str:
        """Renders the PDF and writes it to disk, returning the stored path."""
        pdf_bytes, filename = await self.render_pdf(report)
        path = self.output_path(report, filename)
        path.write_bytes(pdf_bytes)
        return str(path)
