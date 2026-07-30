"""
Standalone async email senders for FastAPI BackgroundTasks / asyncio.create_task.

Every function in this module:
  - Accepts only plain primitive IDs (int / str)
  - Opens its own AsyncSessionLocal session (safe after HTTP response is sent)
  - Fetches ORM objects fresh from the DB
  - Delegates to EmailService (which handles deduplication + logging)
  - Never raises — email failure must never affect the main task action

Usage in API routes (FastAPI BackgroundTasks):
    background_tasks.add_task(bg_send_task_assigned, task_id, assignee_id, assigned_by_id)

Usage inside async service functions (chat_service, scheduler):
    asyncio.create_task(bg_send_task_assigned(task_id, assignee_id, assigned_by_id))
"""
from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models.task_request import TaskRequest
from app.repositories.task_repository import TaskRepository
from app.repositories.user_repository import UserRepository
from app.services.email_service import email_service

logger = logging.getLogger("background_email")


# ─── 1. Task assigned ─────────────────────────────────────────────────────────

async def bg_send_task_assigned(
    task_id: int, assignee_id: int, assigned_by_id: int
) -> None:
    logger.info(
        "bg_send_task_assigned START | task_id=%s | assignee_id=%s | assigned_by_id=%s",
        task_id, assignee_id, assigned_by_id,
    )
    try:
        async with AsyncSessionLocal() as db:
            task = await TaskRepository(db).get_by_id(task_id)
            user_repo = UserRepository(db)
            assignee = await user_repo.get_by_id(assignee_id)
            assigned_by = await user_repo.get_by_id(assigned_by_id)
            if not (task and assignee and assigned_by):
                logger.warning(
                    "bg_send_task_assigned: missing data | task=%s assignee=%s by=%s",
                    task_id, assignee_id, assigned_by_id,
                )
                return
            await email_service.send_task_assigned(
                db, task=task, assignee=assignee, assigned_by=assigned_by,
            )
    except Exception:
        logger.exception("bg_send_task_assigned FAILED | task_id=%s", task_id)


# ─── 2. Due date updated ──────────────────────────────────────────────────────

async def bg_send_due_date_updated(
    task_id: int,
    assignee_id: int,
    updated_by_id: int,
    old_due_date_str: str | None,
) -> None:
    logger.info(
        "bg_send_due_date_updated START | task_id=%s | assignee_id=%s",
        task_id, assignee_id,
    )
    try:
        async with AsyncSessionLocal() as db:
            task = await TaskRepository(db).get_by_id(task_id)
            user_repo = UserRepository(db)
            assignee = await user_repo.get_by_id(assignee_id)
            updated_by = await user_repo.get_by_id(updated_by_id)
            if not (task and assignee and updated_by):
                logger.warning(
                    "bg_send_due_date_updated: missing data | task=%s assignee=%s by=%s",
                    task_id, assignee_id, updated_by_id,
                )
                return
            old_due_date: date | None = (
                date.fromisoformat(old_due_date_str) if old_due_date_str else None
            )
            await email_service.send_due_date_updated(
                db,
                task=task,
                assignee=assignee,
                updated_by=updated_by,
                old_due_date=old_due_date,
            )
    except Exception:
        logger.exception("bg_send_due_date_updated FAILED | task_id=%s", task_id)


# ─── 3. Task sent for review ──────────────────────────────────────────────────

async def bg_send_task_sent_for_review(
    task_id: int, submitter_id: int, reviewer_id: int
) -> None:
    logger.info(
        "bg_send_task_sent_for_review START | task_id=%s | submitter_id=%s | reviewer_id=%s",
        task_id, submitter_id, reviewer_id,
    )
    try:
        async with AsyncSessionLocal() as db:
            task = await TaskRepository(db).get_by_id(task_id)
            user_repo = UserRepository(db)
            submitter = await user_repo.get_by_id(submitter_id)
            reviewer = await user_repo.get_by_id(reviewer_id)
            if not (task and submitter and reviewer):
                logger.warning(
                    "bg_send_task_sent_for_review: missing data"
                    " | task=%s submitter=%s reviewer=%s",
                    task_id, submitter_id, reviewer_id,
                )
                return
            await email_service.send_task_sent_for_review(
                db, task=task, assignee=submitter, manager=reviewer,
            )
    except Exception:
        logger.exception("bg_send_task_sent_for_review FAILED | task_id=%s", task_id)


# ─── 4. Task approved ─────────────────────────────────────────────────────────

async def bg_send_task_approved(
    task_id: int, recipient_id: int, approved_by_id: int
) -> None:
    logger.info(
        "bg_send_task_approved START | task_id=%s | recipient_id=%s | approved_by_id=%s",
        task_id, recipient_id, approved_by_id,
    )
    try:
        async with AsyncSessionLocal() as db:
            task = await TaskRepository(db).get_by_id(task_id)
            user_repo = UserRepository(db)
            recipient = await user_repo.get_by_id(recipient_id)
            approved_by = await user_repo.get_by_id(approved_by_id)
            if not (task and recipient and approved_by):
                logger.warning(
                    "bg_send_task_approved: missing data | task=%s recipient=%s by=%s",
                    task_id, recipient_id, approved_by_id,
                )
                return
            await email_service.send_task_approved(
                db, task=task, assignee=recipient, approved_by=approved_by,
            )
    except Exception:
        logger.exception("bg_send_task_approved FAILED | task_id=%s", task_id)


# ─── 5. Task assigned back ────────────────────────────────────────────────────

async def bg_send_task_assigned_back(
    task_id: int, assignee_id: int, manager_id: int, note: str
) -> None:
    logger.info(
        "bg_send_task_assigned_back START | task_id=%s | assignee_id=%s | manager_id=%s",
        task_id, assignee_id, manager_id,
    )
    try:
        async with AsyncSessionLocal() as db:
            task = await TaskRepository(db).get_by_id(task_id)
            user_repo = UserRepository(db)
            assignee = await user_repo.get_by_id(assignee_id)
            manager = await user_repo.get_by_id(manager_id)
            if not (task and assignee and manager):
                logger.warning(
                    "bg_send_task_assigned_back: missing data"
                    " | task=%s assignee=%s manager=%s",
                    task_id, assignee_id, manager_id,
                )
                return
            await email_service.send_task_assigned_back(
                db, task=task, assignee=assignee, manager=manager, note=note,
            )
    except Exception:
        logger.exception("bg_send_task_assigned_back FAILED | task_id=%s", task_id)


# ─── 6. Due date reminder (scheduler) ────────────────────────────────────────

async def bg_send_due_date_reminder(
    task_id: int, recipient_id: int, window: str, role_label: str
) -> None:
    logger.info(
        "bg_send_due_date_reminder START | task_id=%s | recipient_id=%s"
        " | window=%s | role=%s",
        task_id, recipient_id, window, role_label,
    )
    try:
        async with AsyncSessionLocal() as db:
            task = await TaskRepository(db).get_by_id(task_id)
            recipient = await UserRepository(db).get_by_id(recipient_id)
            if not (task and recipient):
                logger.warning(
                    "bg_send_due_date_reminder: missing data | task=%s recipient=%s",
                    task_id, recipient_id,
                )
                return
            await email_service.send_due_date_reminder(
                db, task=task, recipient=recipient, window=window, role_label=role_label,
            )
    except Exception:
        logger.exception("bg_send_due_date_reminder FAILED | task_id=%s", task_id)


# ─── 7. Client task request submitted ────────────────────────────────────────

async def bg_send_client_task_request(
    task_request_id: int, recipient_id: int, submitted_by_id: int
) -> None:
    logger.info(
        "bg_send_client_task_request START | request_id=%s | recipient_id=%s | submitted_by_id=%s",
        task_request_id, recipient_id, submitted_by_id,
    )
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(TaskRequest).where(TaskRequest.id == task_request_id))
            task_request = result.scalar_one_or_none()
            user_repo = UserRepository(db)
            recipient = await user_repo.get_by_id(recipient_id)
            submitted_by = await user_repo.get_by_id(submitted_by_id)
            if not (task_request and recipient and submitted_by):
                logger.warning(
                    "bg_send_client_task_request: missing data | request=%s recipient=%s submitted_by=%s",
                    task_request_id, recipient_id, submitted_by_id,
                )
                return
            await email_service.send_client_task_request(
                db, task_request=task_request, recipient=recipient, submitted_by=submitted_by,
            )
    except Exception:
        logger.exception("bg_send_client_task_request FAILED | request_id=%s", task_request_id)


# ─── 8. Task request reviewed (approved/rejected) ────────────────────────────

async def bg_send_task_request_reviewed(
    task_request_id: int, client_id: int, reviewed_by_id: int, approved: bool
) -> None:
    logger.info(
        "bg_send_task_request_reviewed START | request_id=%s | client_id=%s | reviewed_by_id=%s | approved=%s",
        task_request_id, client_id, reviewed_by_id, approved,
    )
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(TaskRequest).where(TaskRequest.id == task_request_id))
            task_request = result.scalar_one_or_none()
            user_repo = UserRepository(db)
            client = await user_repo.get_by_id(client_id)
            reviewed_by = await user_repo.get_by_id(reviewed_by_id)
            if not (task_request and client and reviewed_by):
                logger.warning(
                    "bg_send_task_request_reviewed: missing data | request=%s client=%s reviewed_by=%s",
                    task_request_id, client_id, reviewed_by_id,
                )
                return
            await email_service.send_task_request_reviewed(
                db, task_request=task_request, client=client, reviewed_by=reviewed_by, approved=approved,
            )
    except Exception:
        logger.exception("bg_send_task_request_reviewed FAILED | request_id=%s", task_request_id)
