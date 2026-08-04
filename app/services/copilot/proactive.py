"""Proactive Copilot (spec Section 7, bounded slice) — a daily digest for
managers/admins/owners summarizing overdue tasks and workload imbalance
across the teams they oversee, delivered through the existing in-app
Notification channel (same delivery mechanism as the pre-existing due-date
reminders in automation_scheduler.py — no new channel invented)."""

import logging
from collections import defaultdict
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import Notification
from app.models.organization import OrganizationMembership
from app.models.task import Task

logger = logging.getLogger("copilot.proactive")

_RECIPIENT_ROLES = {"owner", "admin", "team_manager"}


async def run_daily_brief(db: AsyncSession) -> int:
    """Runs across every organization in one pass — returns the number of
    briefs sent. Never raises; a failure for one org must not stop the rest."""
    sent = 0
    result = await db.execute(
        select(OrganizationMembership.organization_id).distinct()
    )
    org_ids = [row[0] for row in result.all()]

    for org_id in org_ids:
        try:
            sent += await _run_for_org(db, org_id)
        except Exception:
            logger.exception("Daily copilot brief failed for org=%s", org_id)
    return sent


async def _run_for_org(db: AsyncSession, org_id) -> int:
    today = date.today()

    tasks_result = await db.execute(
        select(Task).where(
            Task.organization_id == org_id,
            Task.status.notin_(["done", "pending_review"]),
        )
    )
    tasks = list(tasks_result.scalars().all())
    overdue = [t for t in tasks if t.due_date and t.due_date < today]
    if not overdue and len(tasks) < 5:
        return 0  # nothing noteworthy — don't spam an empty brief

    by_assignee: dict[int, int] = defaultdict(int)
    for t in tasks:
        if t.assignee_id:
            by_assignee[t.assignee_id] += 1
    imbalance_note = ""
    if by_assignee:
        busiest_id = max(by_assignee, key=by_assignee.get)
        busiest_count = by_assignee[busiest_id]
        avg = sum(by_assignee.values()) / len(by_assignee)
        if busiest_count >= max(3, avg * 2):
            imbalance_note = f" One person is carrying {busiest_count} open tasks — notably more than the team average."

    message = (
        f"{len(overdue)} task(s) are overdue and {len(tasks)} are open org-wide."
        f"{imbalance_note}"
    )

    recipients_result = await db.execute(
        select(OrganizationMembership.user_id).where(
            OrganizationMembership.organization_id == org_id,
            OrganizationMembership.role.in_(_RECIPIENT_ROLES),
        )
    )
    recipient_ids = [row[0] for row in recipients_result.all()]

    sent = 0
    for user_id in recipient_ids:
        existing = await db.execute(
            select(Notification).where(
                Notification.user_id == user_id,
                Notification.title == "Daily copilot brief",
                Notification.type == "copilot_daily_brief",
                Notification.created_at >= today,
            )
        )
        if existing.scalar_one_or_none() is not None:
            continue  # already sent today
        db.add(Notification(
            user_id=user_id,
            title="Daily copilot brief",
            message=message,
            type="copilot_daily_brief",
        ))
        sent += 1

    if sent:
        await db.commit()
    return sent
