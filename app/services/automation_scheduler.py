"""
Automation scheduler — runs recurring jobs directly (no Celery / Redis).

All heavy async work is done inline using AsyncSessionLocal so the app
runs without any Redis or Celery worker process.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.database import AsyncSessionLocal
from app.models.meeting import Meeting
from app.models.notification import Notification
from app.models.task import Task
from app.models.team import Team
from app.models.user import User
from app.repositories.integration_repository import IntegrationRepository
from app.services.background_email import bg_send_due_date_reminder
from app.services.copilot.proactive import run_daily_brief

logger = logging.getLogger("automation_scheduler")

scheduler = AsyncIOScheduler()


# ─── Job 1: in-app due-date notifications ────────────────────────────────────

async def run_due_date_notifications() -> None:
    """Create in-app overdue / due-soon notifications for all assignees."""
    today = date.today()
    due_soon_cutoff = today + timedelta(days=3)

    logger.info("Scheduler: due-date notifications START")
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Task)
                .where(Task.assignee_id.isnot(None))
                .where(Task.due_date.isnot(None))
                .where(Task.status.notin_(["done", "pending_review"]))
            )
            tasks = list(result.scalars().all())
            logger.info("Scheduler: tasks checked for due dates: %s", len(tasks))

            for task in tasks:
                if task.due_date < today:
                    notify_type = "task_overdue"
                    title = "Task overdue"
                    message = (
                        f"Your task '{task.name}' is overdue (was due {task.due_date})."
                    )
                elif today <= task.due_date <= due_soon_cutoff:
                    notify_type = "task_due_soon"
                    title = "Task due soon"
                    message = f"Your task '{task.name}' is due on {task.due_date}."
                else:
                    continue

                # Skip if an unread notification of this type already exists.
                existing = await db.execute(
                    select(Notification).where(
                        Notification.user_id == task.assignee_id,
                        Notification.task_id == task.id,
                        Notification.type == notify_type,
                        Notification.is_read == False,  # noqa: E712
                    )
                )
                if existing.scalar_one_or_none() is not None:
                    continue

                db.add(
                    Notification(
                        user_id=task.assignee_id,
                        task_id=task.id,
                        title=title,
                        message=message,
                        type=notify_type,
                    )
                )
                logger.info(
                    "Scheduler: queued %s notification | user_id=%s task_id=%s",
                    notify_type, task.assignee_id, task.id,
                )

            await db.commit()
    except Exception:
        logger.exception("Scheduler: due-date notifications FAILED")

    logger.info("Scheduler: due-date notifications END")


# ─── Job 2: due-date email reminders ─────────────────────────────────────────

async def run_due_date_email_reminders() -> None:
    """Email assignees and team managers for tasks due today or tomorrow."""
    today = date.today()
    tomorrow = today + timedelta(days=1)

    logger.info("Scheduler: due-date email reminders START")
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Task)
                .where(Task.assignee_id.isnot(None))
                .where(Task.due_date.isnot(None))
                .where(Task.status != "done")
                .where(Task.due_date.in_([today, tomorrow]))
                .options(
                    selectinload(Task.assignee),
                    selectinload(Task.team),
                )
            )
            tasks = list(result.scalars().all())
            logger.info("Scheduler: tasks due today/tomorrow: %s", len(tasks))

            for task in tasks:
                window = "today" if task.due_date == today else "tomorrow"

                # Email the assignee — deduplication is handled inside email_service.
                if task.assignee_id:
                    asyncio.create_task(
                        bg_send_due_date_reminder(
                            task.id, task.assignee_id, window, "assignee"
                        )
                    )

                # Email the team manager (if different from assignee).
                if task.team_id:
                    team_result = await db.execute(
                        select(Team).where(Team.id == task.team_id)
                    )
                    team = team_result.scalar_one_or_none()
                    if (
                        team
                        and team.team_manager_id
                        and team.team_manager_id != task.assignee_id
                    ):
                        asyncio.create_task(
                            bg_send_due_date_reminder(
                                task.id, team.team_manager_id, window, "manager"
                            )
                        )
    except Exception:
        logger.exception("Scheduler: due-date email reminders FAILED")

    logger.info("Scheduler: due-date email reminders END")


# ─── Job 3: daily AI task sync (Microsoft) ───────────────────────────────────

async def run_daily_ai_task_sync() -> None:
    """
    For each active user with a Microsoft account, run the data sync and
    AI extraction directly (no Celery enqueuing).
    """
    from app.services.automation_tasks import (
        analyze_yesterday_sources_for_user,
        resolve_org_id_for_user,
        sync_microsoft_data_for_user,
    )

    logger.info("Scheduler: AI task sync START")
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(User).where(User.is_active.is_(True))
            )
            users = list(result.scalars().all())
            logger.info("Scheduler: active users found: %s", len(users))

            for user in users:
                try:
                    org_id = await resolve_org_id_for_user(db, user)
                    if org_id is None:
                        logger.info(
                            "Scheduler: skipping user %s — no organization context.",
                            user.email,
                        )
                        continue

                    integration_repo = IntegrationRepository(db, org_id)
                    microsoft_accounts = await integration_repo.list_accounts_by_provider(
                        user.id, "microsoft"
                    )
                    if not microsoft_accounts:
                        logger.info(
                            "Scheduler: skipping user %s — no Microsoft account.",
                            user.email,
                        )
                        continue

                    logger.info(
                        "Scheduler: syncing Microsoft data | user_id=%s email=%s",
                        user.id, user.email,
                    )
                    sync_result = await sync_microsoft_data_for_user(db=db, user=user)
                    logger.info(
                        "Scheduler: sync done | user_id=%s | emails=%s events=%s transcripts=%s",
                        user.id,
                        sync_result.get("emails_imported", 0),
                        sync_result.get("calendar_events_imported", 0),
                        sync_result.get("transcripts_imported", 0),
                    )

                    ai_result = await analyze_yesterday_sources_for_user(db=db, user=user, org_id=org_id)
                    logger.info(
                        "Scheduler: AI extraction done | user_id=%s"
                        " | sources=%s tasks_created=%s",
                        user.id,
                        ai_result.get("sources_analyzed", 0),
                        ai_result.get("tasks_created", 0),
                    )

                except Exception:
                    logger.exception(
                        "Scheduler: AI task sync FAILED for user_id=%s email=%s",
                        user.id, user.email,
                    )
    except Exception:
        logger.exception("Scheduler: AI task sync outer FAILED")

    logger.info("Scheduler: AI task sync END")


# ─── Job: proactive daily copilot brief ──────────────────────────────────────

async def run_daily_copilot_brief() -> None:
    """Proactive Copilot (spec Section 7) — overdue/workload digest for
    owners/admins/team_managers, delivered as an in-app notification."""
    logger.info("Scheduler: daily copilot brief START")
    try:
        async with AsyncSessionLocal() as db:
            sent = await run_daily_brief(db)
            logger.info("Scheduler: daily copilot brief sent to %s recipient(s)", sent)
    except Exception:
        logger.exception("Scheduler: daily copilot brief failed")
    logger.info("Scheduler: daily copilot brief END")


# ─── Job: meeting start reminders ────────────────────────────────────────────

async def run_meeting_start_reminders() -> None:
    """In-app reminder for every assigned attendee once a scheduled
    meeting's start time arrives. Runs every minute; `Meeting.reminder_sent_at`
    guards against re-notifying on every subsequent tick once a meeting's
    reminder has fired, and the 10-minute lookback window means a brief
    scheduler restart around the exact minute doesn't silently skip one."""
    now = datetime.now(timezone.utc)
    lookback = now - timedelta(minutes=10)

    logger.info("Scheduler: meeting start reminders START")
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Meeting)
                .where(Meeting.status == "scheduled")
                .where(Meeting.scheduled_at <= now)
                .where(Meeting.scheduled_at >= lookback)
                .where(Meeting.reminder_sent_at.is_(None))
                .options(selectinload(Meeting.participants))
            )
            meetings = list(result.scalars().all())
            logger.info("Scheduler: meetings due for a start reminder: %s", len(meetings))

            for meeting in meetings:
                recipient_ids = {p.user_id for p in meeting.participants if p.user_id is not None}
                for user_id in recipient_ids:
                    db.add(
                        Notification(
                            user_id=user_id,
                            meeting_id=meeting.id,
                            title="Meeting starting now",
                            message=f'"{meeting.title}" is scheduled to start now.',
                            type="meeting_reminder",
                        )
                    )
                meeting.reminder_sent_at = now
                logger.info(
                    "Scheduler: queued meeting_reminder | meeting_id=%s recipients=%s",
                    meeting.id, len(recipient_ids),
                )

            await db.commit()
    except Exception:
        logger.exception("Scheduler: meeting start reminders FAILED")

    logger.info("Scheduler: meeting start reminders END")


# ─── Scheduler setup ──────────────────────────────────────────────────────────

def start_scheduler() -> None:
    if scheduler.running:
        logger.info("Automation scheduler already running.")
        return

    # AI task sync — every 5 min in dev; switch to cron(hour=6) for production.
    scheduler.add_job(
        run_daily_ai_task_sync,
        trigger="interval",
        minutes=5,
        id="daily_ai_task_sync",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # In-app due-date notifications — every hour.
    scheduler.add_job(
        run_due_date_notifications,
        trigger="interval",
        hours=1,
        id="due_date_notifications",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Due-date email reminders — daily at 08:00 UTC.
    scheduler.add_job(
        run_due_date_email_reminders,
        trigger="cron",
        hour=8,
        minute=0,
        id="due_date_email_reminders",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Proactive daily copilot brief — daily at 07:00 UTC, ahead of the
    # due-date email reminders above.
    scheduler.add_job(
        run_daily_copilot_brief,
        trigger="cron",
        hour=7,
        minute=0,
        id="daily_copilot_brief",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Meeting start reminders — every minute, so "when the scheduled time
    # arrives" is tight enough to feel immediate.
    scheduler.add_job(
        run_meeting_start_reminders,
        trigger="interval",
        minutes=1,
        id="meeting_start_reminders",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    scheduler.start()
    logger.info("Automation scheduler started (no Celery/Redis required).")


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Automation scheduler stopped.")
