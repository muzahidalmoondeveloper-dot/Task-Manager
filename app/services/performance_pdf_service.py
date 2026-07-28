"""Renders Employee/Team Performance PDF reports.

Deliberately parallel to, not merged with, `report_generation_service.py` /
`pdf_render_service.py`'s `render_html` — those are hard-wired to Rocks/KPIs/
Tasks-by-project and not worth force-fitting. This module reuses only the
generic pieces: the Playwright worker (`PdfRenderService.render_html_string`)
and the `_slugify` filename helper.

The one non-negotiable rule (source spec §34): every number here comes from
`scoreboard_service.build_employee_scoreboard` / `build_team_scoreboard` —
the exact same functions the live Scoreboard API routes call. This module
never recomputes a score.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.models.report import Report
from app.repositories.team_repository import TeamRepository
from app.services import scoreboard_service as scoring
from app.services.pdf_render_service import PdfRenderService, _slugify

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "reports"
_PRINT_CSS_PATH = _TEMPLATE_DIR / "report_print.css"

_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),
)


def _summary_dict(result: scoring.ScoreboardResult) -> dict:
    return {
        "total_assigned": result.total_assigned,
        "total_completed": result.total_completed,
        "completed_before_due": result.completed_before_due,
        "completed_on_due": result.completed_on_due,
        "completed_after_due": result.completed_after_due,
        "completed_no_due_date": result.completed_no_due_date,
        "overdue": result.overdue,
        "pending": result.pending,
        "completion_rate": result.completion_rate,
        "on_time_rate": result.on_time_rate,
    }


def _score_dict(result: scoring.ScoreboardResult, change_from_previous: int | None) -> dict:
    return {
        "has_data": result.has_data,
        "rounded_score": result.rounded_score,
        "performance_level": result.performance_level,
        "change_from_previous": change_from_previous,
    }


def build_performance_filename(kind: str, subject_slug: str, period_start: date | None, period_end: date | None) -> str:
    """Spec §22: `employee-performance-{name}-{start}-{end}.pdf` /
    `team-performance-{name}-{start}-{end}.pdf`."""
    start_slug = period_start.isoformat() if period_start else "period"
    end_slug = period_end.isoformat() if period_end else "period"
    return f"{kind}-performance-{_slugify(subject_slug)}-{start_slug}-{end_slug}.pdf"


async def generate_employee_pdf(
    db, org, employee, generated_by_name: str, report: Report, *,
    period: str, project_id: int | None, team_id: int | None,
    start_date: date | None, end_date: date | None, include_task_details: bool,
) -> tuple[bytes, dict]:
    """Returns (pdf_bytes, performance_snapshot_dict)."""
    data = await scoring.build_employee_scoreboard(
        db, org.id, employee.id, period, project_id, team_id, start_date, end_date,
    )

    team_repo_teams = []
    all_teams = await TeamRepository(db, org.id).list_all()
    for t in all_teams:
        if t.team_manager_id == employee.id or any(m.user_id == employee.id for m in t.memberships):
            team_repo_teams.append(t.name)

    tasks = []
    if include_task_details:
        tasks = await scoring.build_employee_task_items(
            db, org.id, employee.id, data.period_start, data.period_end, project_id, team_id,
        )

    snapshot = {
        "period": data.period,
        "period_start": data.period_start.isoformat(),
        "period_end": data.period_end.isoformat(),
        "project_id": project_id,
        "team_id": team_id,
        "summary": _summary_dict(data.current),
        "score": _score_dict(data.current, data.change_from_previous),
        "explanation": data.explanation,
        "include_task_details": include_task_details,
        "task_count": len(tasks),
    }

    template = _env.get_template("performance_employee_report.html")
    html = template.render(
        report=report,
        org={"name": org.name, "logo_url": org.logo_url},
        employee={"full_name": employee.full_name, "role": employee.role, "teams": team_repo_teams},
        summary=_summary_dict(data.current),
        score=_score_dict(data.current, data.change_from_previous),
        explanation=data.explanation,
        tasks=tasks,
        include_task_details=include_task_details,
        generated_date=date.today().isoformat(),
        generated_by=generated_by_name,
        print_css=_PRINT_CSS_PATH.read_text(encoding="utf-8"),
    )

    pdf_bytes = await PdfRenderService().render_html_string(html)
    return pdf_bytes, snapshot


async def generate_team_pdf(
    db, org, team, generated_by_name: str, report: Report, *,
    period: str, project_id: int | None,
    start_date: date | None, end_date: date | None, include_task_details: bool,
) -> tuple[bytes, dict]:
    """Returns (pdf_bytes, performance_snapshot_dict)."""
    data = await scoring.build_team_scoreboard(db, org.id, team, period, project_id, start_date, end_date)

    members = [
        {
            "rank": m.rank,
            "full_name": m.full_name,
            "role": m.role,
            "has_data": m.result.has_data,
            "rounded_score": m.result.rounded_score if m.result.has_data else None,
            "performance_level": m.result.performance_level if m.result.has_data else None,
            "total_assigned": m.result.total_assigned,
            "total_completed": m.result.total_completed,
            "overdue": m.result.overdue,
            "on_time_rate": m.result.on_time_rate,
        }
        for m in data.members
    ]

    tasks = []
    if include_task_details:
        tasks = await scoring.build_team_task_items(
            db, org.id, team.id, data.period_start, data.period_end, project_id,
        )

    snapshot = {
        "period": data.period,
        "period_start": data.period_start.isoformat(),
        "period_end": data.period_end.isoformat(),
        "project_id": project_id,
        "summary": _summary_dict(data.current),
        "score": _score_dict(data.current, data.change_from_previous),
        "members": members,
        "insights": data.insights,
        "include_task_details": include_task_details,
        "task_count": len(tasks),
    }

    template = _env.get_template("performance_team_report.html")
    html = template.render(
        report=report,
        org={"name": org.name, "logo_url": org.logo_url},
        team={"name": team.name, "manager_name": team.team_manager.full_name if team.team_manager else None, "member_count": len(team.memberships)},
        summary=_summary_dict(data.current),
        score=_score_dict(data.current, data.change_from_previous),
        members=members,
        insights=data.insights,
        tasks=tasks,
        include_task_details=include_task_details,
        generated_date=date.today().isoformat(),
        generated_by=generated_by_name,
        print_css=_PRINT_CSS_PATH.read_text(encoding="utf-8"),
    )

    pdf_bytes = await PdfRenderService().render_html_string(html)
    return pdf_bytes, snapshot
