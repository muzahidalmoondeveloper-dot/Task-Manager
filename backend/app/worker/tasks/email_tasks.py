"""
ARCHIVED — NOT USED IN THE ACTIVE CODE PATH
============================================
Email sending has been migrated to FastAPI BackgroundTasks via
app.services.background_email.  These Celery task wrappers are kept for
reference in case Celery is re-enabled.  Nothing in the active application
imports from this module.

Original purpose: Celery tasks for all email notifications.
Each task accepted plain IDs, re-fetched ORM objects in a WorkerSession,
and delegated to EmailService (which handles deduplication).
"""
import asyncio
import logging
from datetime import date

from app.worker.celery_app import celery_app
from app.worker.database import WorkerSession
from app.repositories.task_repository import TaskRepository
from app.repositories.user_repository import UserRepository
from app.services.email_service import email_service

logger = logging.getLogger("celery.email_tasks")

# ─── Retry defaults shared by every email task ────────────────────────────────
_RETRY = dict(
    max_retries=3,
    autoretry_for=(Exception,),
    retry_backoff=True,       # 1 min, 2 min, 4 min …
    retry_backoff_max=600,    # cap at 10 minutes
    retry_jitter=True,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def _fetch_task_and_users(task_id: int, *user_ids: int):
    """Open a WorkerSession and return (task, *users) — any missing → None."""
    async with WorkerSession() as db:
        task = await TaskRepository(db).get_by_id(task_id)
        users = [await UserRepository(db).get_by_id(uid) for uid in user_ids]
        return (task, *users)


# ─── 1. Task assigned ─────────────────────────────────────────────────────────

@celery_app.task(bind=True, name="app.worker.tasks.email_tasks.send_task_assigned_email", **_RETRY)
def send_task_assigned_email(self, task_id: int, assignee_id: int, assigned_by_id: int):
    logger.info(
        "TASK START send_task_assigned_email | task_id=%s | assignee_id=%s | assigned_by_id=%s",
        task_id, assignee_id, assigned_by_id,
    )
    asyncio.run(_do_send_task_assigned(task_id, assignee_id, assigned_by_id))


async def _do_send_task_assigned(task_id: int, assignee_id: int, assigned_by_id: int):
    logger.info(
        "DO send_task_assigned | task_id=%s | assignee_id=%s | assigned_by_id=%s",
        task_id, assignee_id, assigned_by_id,
    )
    async with WorkerSession() as db:
        task = await TaskRepository(db).get_by_id(task_id)
        user_repo = UserRepository(db)
        assignee = await user_repo.get_by_id(assignee_id)
        assigned_by = await user_repo.get_by_id(assigned_by_id)

        if not (task and assignee and assigned_by):
            logger.warning(
                "send_task_assigned: missing data | task=%s assignee=%s by=%s",
                task_id, assignee_id, assigned_by_id,
            )
            return

        logger.info(
            "send_task_assigned: sending email | task=%s | to=%s | by=%s",
            task_id, assignee.email, assigned_by.email,
        )
        await email_service.send_task_assigned(
            db, task=task, assignee=assignee, assigned_by=assigned_by,
        )


# ─── 2. Due date updated ──────────────────────────────────────────────────────

@celery_app.task(bind=True, name="app.worker.tasks.email_tasks.send_due_date_updated_email", **_RETRY)
def send_due_date_updated_email(
    self,
    task_id: int,
    assignee_id: int,
    updated_by_id: int,
    old_due_date_str: str | None,   # "YYYY-MM-DD" or None
):
    logger.info(
        "TASK START send_due_date_updated_email | task_id=%s | assignee_id=%s",
        task_id, assignee_id,
    )
    asyncio.run(_do_send_due_date_updated(task_id, assignee_id, updated_by_id, old_due_date_str))


async def _do_send_due_date_updated(
    task_id: int,
    assignee_id: int,
    updated_by_id: int,
    old_due_date_str: str | None,
):
    old_due_date: date | None = (
        date.fromisoformat(old_due_date_str) if old_due_date_str else None
    )

    async with WorkerSession() as db:
        task = await TaskRepository(db).get_by_id(task_id)
        user_repo = UserRepository(db)
        assignee = await user_repo.get_by_id(assignee_id)
        updated_by = await user_repo.get_by_id(updated_by_id)

        if not (task and assignee and updated_by):
            logger.warning(
                "send_due_date_updated: missing data | task=%s assignee=%s by=%s",
                task_id, assignee_id, updated_by_id,
            )
            return

        await email_service.send_due_date_updated(
            db,
            task=task,
            assignee=assignee,
            updated_by=updated_by,
            old_due_date=old_due_date,
        )


# ─── 3. Task sent for review ──────────────────────────────────────────────────

@celery_app.task(bind=True, name="app.worker.tasks.email_tasks.send_task_sent_for_review_email", **_RETRY)
def send_task_sent_for_review_email(self, task_id: int, submitter_id: int, reviewer_id: int):
    logger.info(
        "TASK START send_task_sent_for_review_email | task_id=%s | submitter_id=%s | reviewer_id=%s",
        task_id, submitter_id, reviewer_id,
    )
    asyncio.run(_do_send_task_sent_for_review(task_id, submitter_id, reviewer_id))


async def _do_send_task_sent_for_review(task_id: int, submitter_id: int, reviewer_id: int):
    async with WorkerSession() as db:
        task = await TaskRepository(db).get_by_id(task_id)
        user_repo = UserRepository(db)
        submitter = await user_repo.get_by_id(submitter_id)
        reviewer = await user_repo.get_by_id(reviewer_id)

        if not (task and submitter and reviewer):
            logger.warning(
                "send_task_sent_for_review: missing data | task=%s submitter=%s reviewer=%s",
                task_id, submitter_id, reviewer_id,
            )
            return

        await email_service.send_task_sent_for_review(
            db, task=task, assignee=submitter, manager=reviewer,
        )


# ─── 4. Task approved ────────────────────────────────────────────────────────

@celery_app.task(bind=True, name="app.worker.tasks.email_tasks.send_task_approved_email", **_RETRY)
def send_task_approved_email(self, task_id: int, recipient_id: int, approved_by_id: int):
    logger.info(
        "TASK START send_task_approved_email | task_id=%s | recipient_id=%s | approved_by_id=%s",
        task_id, recipient_id, approved_by_id,
    )
    asyncio.run(_do_send_task_approved(task_id, recipient_id, approved_by_id))


async def _do_send_task_approved(task_id: int, recipient_id: int, approved_by_id: int):
    async with WorkerSession() as db:
        task = await TaskRepository(db).get_by_id(task_id)
        user_repo = UserRepository(db)
        recipient = await user_repo.get_by_id(recipient_id)
        approved_by = await user_repo.get_by_id(approved_by_id)

        if not (task and recipient and approved_by):
            logger.warning(
                "send_task_approved: missing data | task=%s recipient=%s by=%s",
                task_id, recipient_id, approved_by_id,
            )
            return

        await email_service.send_task_approved(
            db, task=task, assignee=recipient, approved_by=approved_by,
        )


# ─── 5. Task assigned back ────────────────────────────────────────────────────

@celery_app.task(bind=True, name="app.worker.tasks.email_tasks.send_task_assigned_back_email", **_RETRY)
def send_task_assigned_back_email(self, task_id: int, assignee_id: int, manager_id: int, note: str):
    logger.info(
        "TASK START send_task_assigned_back_email | task_id=%s | assignee_id=%s | manager_id=%s",
        task_id, assignee_id, manager_id,
    )
    asyncio.run(_do_send_task_assigned_back(task_id, assignee_id, manager_id, note))


async def _do_send_task_assigned_back(task_id: int, assignee_id: int, manager_id: int, note: str):
    async with WorkerSession() as db:
        task = await TaskRepository(db).get_by_id(task_id)
        user_repo = UserRepository(db)
        assignee = await user_repo.get_by_id(assignee_id)
        manager = await user_repo.get_by_id(manager_id)

        if not (task and assignee and manager):
            logger.warning(
                "send_task_assigned_back: missing data | task=%s assignee=%s manager=%s",
                task_id, assignee_id, manager_id,
            )
            return

        await email_service.send_task_assigned_back(
            db, task=task, assignee=assignee, manager=manager, note=note,
        )


# ─── 6. Due date reminder ─────────────────────────────────────────────────────

@celery_app.task(bind=True, name="app.worker.tasks.email_tasks.send_due_date_reminder_email", **_RETRY)
def send_due_date_reminder_email(
    self,
    task_id: int,
    recipient_id: int,
    window: str,        # "today" | "tomorrow"
    role_label: str,    # "assignee" | "manager"
):
    logger.info(
        "TASK START send_due_date_reminder_email | task_id=%s | recipient_id=%s | window=%s | role=%s",
        task_id, recipient_id, window, role_label,
    )
    asyncio.run(_do_send_due_date_reminder(task_id, recipient_id, window, role_label))


async def _do_send_due_date_reminder(
    task_id: int, recipient_id: int, window: str, role_label: str,
):
    async with WorkerSession() as db:
        task = await TaskRepository(db).get_by_id(task_id)
        recipient = await UserRepository(db).get_by_id(recipient_id)

        if not (task and recipient):
            logger.warning(
                "send_due_date_reminder: missing data | task=%s recipient=%s",
                task_id, recipient_id,
            )
            return

        await email_service.send_due_date_reminder(
            db, task=task, recipient=recipient, window=window, role_label=role_label,
        )
