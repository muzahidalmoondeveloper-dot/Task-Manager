"""
ARCHIVED — NOT USED IN THE ACTIVE CODE PATH
============================================
The application has migrated from Celery + Redis to FastAPI BackgroundTasks
(for real-time email events) and APScheduler running jobs directly in-process
(for recurring jobs).  This file is kept for reference in case Celery is
re-enabled in the future.  Nothing in the active application imports from
this module.
"""
import logging

from celery import Celery
from celery.signals import task_failure, task_prerun, task_success

from app.core.config import settings

logger = logging.getLogger("celery.app")

celery_app = Celery(
    "task_manager",
    broker=settings.celery_broker,
    backend=settings.celery_backend,
    include=[
        "app.worker.tasks.email_tasks",
        "app.worker.tasks.sync_tasks",
        "app.worker.tasks.notification_tasks",
    ],
)

celery_app.conf.update(
    # Serialisation
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],

    # Time zone
    timezone="UTC",
    enable_utc=True,

    # Reliability: only ACK after the task completes, so a crashed worker
    # puts the task back on the queue.
    task_acks_late=True,
    task_reject_on_worker_lost=True,

    # Keep workers from prefetching many tasks at once — important for
    # long-running sync/AI jobs.
    worker_prefetch_multiplier=1,

    # Track STARTED state so the result backend shows in-progress tasks.
    task_track_started=True,

    # Time limits: soft sends SIGTERM so the task can clean up; hard SIGKILL.
    task_soft_time_limit=300,   # 5 minutes
    task_time_limit=600,        # 10 minutes

    # Result TTL: keep results for 24 hours then auto-delete.
    result_expires=86400,

    task_default_queue="default",

    # Windows compatibility: the default prefork pool uses Unix fork() which
    # does not exist on Windows and causes WinError 6 / billiard crashes.
    # "solo" runs tasks in the main thread sequentially — safe on all platforms.
    # On Linux/Mac in production you can override this via the -P flag:
    #   celery -A app.worker.celery_app worker --pool=prefork
    worker_pool="solo",
)


# ─── Startup diagnostic ───────────────────────────────────────────────────────
# Logged once when the module is imported (worker start or FastAPI start).
# Never logs the password — only enough to confirm the right .env was loaded.
logger.info(
    "SMTP config loaded | host=%s | port=%s | username=%s | from_email=%s | env_file=%s",
    settings.SMTP_HOST,
    settings.SMTP_PORT,
    settings.SMTP_USERNAME,
    settings.SMTP_FROM_EMAIL,
    settings.model_config.get("env_file", "unknown"),
)


# ─── Celery signals for structured logging ────────────────────────────────────

@task_prerun.connect
def on_task_prerun(task_id, task, args, kwargs, **_):
    logger.info(
        "CELERY START | task=%s | id=%s | args=%s | kwargs=%s",
        task.name, task_id, args, kwargs,
    )


@task_success.connect
def on_task_success(sender, result, **_):
    logger.info("CELERY SUCCESS | task=%s", sender.name)


@task_failure.connect
def on_task_failure(task_id, exception, traceback, sender, **_):
    logger.error(
        "CELERY FAILURE | task=%s | id=%s | error=%s",
        sender.name, task_id, exception,
    )
