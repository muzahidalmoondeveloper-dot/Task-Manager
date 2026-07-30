"""
ARCHIVED — NOT USED IN THE ACTIVE CODE PATH
============================================
In-app due-date notifications and due-date email reminders are now handled
directly inside APScheduler jobs in app.services.automation_scheduler — no
Celery worker is required.  This file is kept for reference in case Celery is
re-enabled.  Nothing in the active application imports from this module.

Original purpose: Celery tasks for in-app due-date notifications and
due-date email reminders, previously enqueued by the APScheduler jobs.
"""
import asyncio
import logging
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.worker.celery_app import celery_app
from app.worker.database import WorkerSession
from app.models.notification import Notification
from app.models.task import Task
from app.models.team import Team
from app.models.user import User
from app.worker.tasks.email_tasks import send_due_date_reminder_email

logger = logging.getLogger("celery.notification_tasks")


# ─── 1. In-app due-date notifications ────────────────────────────────────────

@celery_app.task(
    bind=True,
    name="app.worker.tasks.notification_tasks.run_due_date_notifications_task",
    max_retries=1,
    autoretry_for=(Exception,),
    retry_backoff=True,
)
def run_due_date_notifications_task(self):
    asyncio.run(_do_due_date_notifications())


async def _do_due_date_notifications():
    today = date.today()
    due_soon_cutoff = today + timedelta(days=3)

    logger.info("DUE DATE NOTIFICATIONS START")

    async with WorkerSession() as db:
        result = await db.execute(
            select(Task)
            .where(Task.assignee_id.isnot(None))
            .where(Task.due_date.isnot(None))
            .where(Task.status.notin_(["done", "pending_review"]))
        )
        tasks = list(result.scalars().all())

        logger.info("Tasks checked for due dates: %s", len(tasks))

        for task in tasks:
            if task.due_date < today:
                notify_type = "task_overdue"
                title = "Task overdue"
                message = f"Your task '{task.name}' is overdue (was due {task.due_date})."
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
                "Queued %s notification | user_id=%s task_id=%s",
                notify_type, task.assignee_id, task.id,
            )

        await db.commit()

    logger.info("DUE DATE NOTIFICATIONS END")


# ─── 2. Due-date email reminders ──────────────────────────────────────────────

@celery_app.task(
    bind=True,
    name="app.worker.tasks.notification_tasks.run_due_date_email_reminders_task",
    max_retries=1,
    autoretry_for=(Exception,),
    retry_backoff=True,
)
def run_due_date_email_reminders_task(self):
    asyncio.run(_do_due_date_email_reminders())


async def _do_due_date_email_reminders():
    today = date.today()
    tomorrow = today + timedelta(days=1)

    logger.info("DUE DATE EMAIL REMINDERS START")

    async with WorkerSession() as db:
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

        logger.info("Tasks due today/tomorrow: %s", len(tasks))

        for task in tasks:
            window = "today" if task.due_date == today else "tomorrow"

            # Enqueue individual email Celery tasks — each handles its own dedup.
            if task.assignee_id:
                send_due_date_reminder_email.delay(
                    task.id, task.assignee_id, window, "assignee",
                )

            if task.team_id:
                team_result = await db.execute(
                    select(Team).where(Team.id == task.team_id)
                )
                team = team_result.scalar_one_or_none()

                if team and team.team_manager_id and team.team_manager_id != task.assignee_id:
                    send_due_date_reminder_email.delay(
                        task.id, team.team_manager_id, window, "manager",
                    )

    logger.info("DUE DATE EMAIL REMINDERS END")
