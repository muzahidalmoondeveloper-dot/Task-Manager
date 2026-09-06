from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.task import Task
from app.models.task_time_entry import TaskTimeEntry
from app.repositories.base_tenant_repository import TenantRepository


class DuplicateActiveTimerError(Exception):
    """Raised when a Start would create a second active timer for the same
    user — either because the application-level check already found one,
    or because the database's partial unique index rejected a concurrent
    insert that raced past that check (see the migration/model comments)."""


class TaskTimeEntryRepository(TenantRepository):
    def __init__(self, db: AsyncSession, org_id) -> None:
        super().__init__(db, org_id)

    async def get_active_for_user(self, user_id: int) -> TaskTimeEntry | None:
        """The user's one active (unstopped) timer anywhere in the org, if
        any — scoped by organization_id, not just user_id, so this can
        never return another tenant's row even though user ids are global."""
        stmt = select(TaskTimeEntry).where(
            TaskTimeEntry.organization_id == self.org_id,
            TaskTimeEntry.user_id == user_id,
            TaskTimeEntry.stopped_at.is_(None),
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_active_for_user_on_task(self, user_id: int, task_id: int) -> TaskTimeEntry | None:
        stmt = select(TaskTimeEntry).where(
            TaskTimeEntry.organization_id == self.org_id,
            TaskTimeEntry.user_id == user_id,
            TaskTimeEntry.task_id == task_id,
            TaskTimeEntry.stopped_at.is_(None),
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def start(self, task_id: int, user_id: int) -> TaskTimeEntry:
        """Creates a new active entry. Raises DuplicateActiveTimerError
        (instead of a raw IntegrityError leaking out) if the partial unique
        index rejects this because a concurrent request already created
        one — the caller doesn't need to know this is a DB-level guarantee
        rather than the earlier application-level check."""
        entry = TaskTimeEntry(
            task_id=task_id,
            user_id=user_id,
            organization_id=self.org_id,
            started_at=datetime.now(timezone.utc),
        )
        self.db.add(entry)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise DuplicateActiveTimerError from exc
        await self.db.refresh(entry)
        return entry

    async def stop(self, entry: TaskTimeEntry) -> TaskTimeEntry:
        """Stops an already-loaded active entry. Duration is computed
        server-side from server-generated timestamps only — never from a
        client-supplied value."""
        stopped_at = datetime.now(timezone.utc)
        entry.stopped_at = stopped_at
        started_at = entry.started_at
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        duration = int((stopped_at - started_at).total_seconds())
        # A clock/precision edge case (e.g. sub-millisecond same-instant
        # start+stop) should floor at zero, never go negative.
        entry.duration_seconds = max(duration, 0)
        await self.db.commit()
        await self.db.refresh(entry)
        return entry

    async def get_active_entries_for_task(self, task_id: int) -> list[TaskTimeEntry]:
        """Every currently-active (unstopped) session on this one task,
        across ALL users — not just the caller's own. Bounded by how many
        users can simultaneously be running a timer on this single task
        (#7A guarantees one active timer per user, not per task — see
        Phase 3 of the spec — so this is normally a tiny list), never by
        historical volume. Callers needing only "is it my timer" should
        filter this list by user_id themselves rather than adding a
        separate single-purpose query."""
        stmt = select(TaskTimeEntry).where(
            TaskTimeEntry.organization_id == self.org_id,
            TaskTimeEntry.task_id == task_id,
            TaskTimeEntry.stopped_at.is_(None),
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def total_completed_seconds(self, task_id: int) -> int:
        """SUM of completed sessions for one task — a single aggregate
        query, not a per-entry Python loop, so this stays cheap regardless
        of how many historical sessions a task has accumulated."""
        stmt = select(func.coalesce(func.sum(TaskTimeEntry.duration_seconds), 0)).where(
            TaskTimeEntry.organization_id == self.org_id,
            TaskTimeEntry.task_id == task_id,
            TaskTimeEntry.stopped_at.is_not(None),
        )
        result = await self.db.execute(stmt)
        return int(result.scalar_one())

    async def get_project_time_summary(self, project_id: int, now: datetime) -> tuple[int, int]:
        """Project Working Time (#7B): `SUM(duration_seconds)` for every
        completed TaskTimeEntry whose Task currently belongs to this
        project, plus the live elapsed time of every entry still active on
        one of those tasks, computed against the caller-supplied `now`
        (so the same instant backs both the returned total and
        `calculated_at` in the API response — see ProjectTimeSummary).

        Two queries, not N+1 and not one per task:
          1. a single SQL SUM for completed sessions (join Task only to
             filter by project_id — nothing is summed in Python).
          2. the (normally small — bounded by "one active timer per user"
             from #7A, so at most as many rows as there are org members
             simultaneously working) set of currently-active entries on
             this project's tasks, whose *elapsed* time can't be computed
             in SQL without depending on the DB's own clock, which would
             disagree with the `now` this response's `calculated_at`
             claims — trivial arithmetic over that small row set instead.

        Both queries filter `Task.organization_id == self.org_id` in
        addition to `TaskTimeEntry.organization_id == self.org_id` and
        `Task.project_id == project_id` — a task with `project_id IS NULL`
        never matches the equality filter, a task from another project is
        excluded by the same filter, and a task from another organization
        is excluded twice over (its own row's organization_id, and,
        structurally, `self.org_id` is always the caller's own tenant —
        never taken from the request). `TaskTimeEntry.task_id` has no
        historical project snapshot: if a task's `project_id` is changed
        after time was tracked on it, that already-recorded time follows
        the task to its new project on the very next read (see the final
        report's "Task Project-Reassignment Semantics" section — this is a
        documented, deliberate consequence of the current data model, not
        a bug introduced here).

        Returns (working_time_seconds, active_timer_count).
        """
        completed_stmt = (
            select(func.coalesce(func.sum(TaskTimeEntry.duration_seconds), 0))
            .select_from(TaskTimeEntry)
            .join(Task, Task.id == TaskTimeEntry.task_id)
            .where(
                TaskTimeEntry.organization_id == self.org_id,
                Task.organization_id == self.org_id,
                Task.project_id == project_id,
                TaskTimeEntry.stopped_at.is_not(None),
            )
        )
        completed_result = await self.db.execute(completed_stmt)
        completed_seconds = int(completed_result.scalar_one())

        active_stmt = (
            select(TaskTimeEntry.started_at)
            .select_from(TaskTimeEntry)
            .join(Task, Task.id == TaskTimeEntry.task_id)
            .where(
                TaskTimeEntry.organization_id == self.org_id,
                Task.organization_id == self.org_id,
                Task.project_id == project_id,
                TaskTimeEntry.stopped_at.is_(None),
            )
        )
        active_result = await self.db.execute(active_stmt)
        active_started_ats = active_result.scalars().all()

        active_elapsed_seconds = 0
        for started_at in active_started_ats:
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=timezone.utc)
            active_elapsed_seconds += max(int((now - started_at).total_seconds()), 0)

        return completed_seconds + active_elapsed_seconds, len(active_started_ats)

    async def get_task_time_summaries(self, task_ids: list[int], now: datetime) -> dict[int, tuple[int, int]]:
        """Bulk equivalent of `total_completed_seconds` + the active-elapsed
        logic in `_build_time_state`/`get_project_time_summary`, for many
        tasks at once — this is what makes Task-list Working Time NOT cost
        one `GET /tasks/{id}/time` per row (Task List Working Time
        Visibility, Phase 4/5 of the spec). Exactly two queries, regardless
        of how many task_ids are passed in:

          1. one SQL `GROUP BY task_id` SUM over completed sessions —
             historical volume is aggregated in the database, never loaded
             into Python row-by-row.
          2. one SELECT of still-active sessions on these tasks — bounded
             by how many timers are concurrently running (across however
             many users, on however many of these tasks), never by
             historical volume — whose elapsed time is then computed in
             Python against the single caller-supplied `now`, exactly like
             `get_project_time_summary` does, so every task's number and
             the response's own `calculated_at` agree on the same instant.

        Returns {task_id: (working_time_seconds, active_timer_count)} —
        only for task_ids that actually have at least one entry; a task
        with zero entries simply has no key here (callers default it to
        (0, 0), matching Working Time's zero-state, see TaskTimeState /
        the task-list bulk endpoint).
        """
        if not task_ids:
            return {}

        completed_stmt = (
            select(TaskTimeEntry.task_id, func.coalesce(func.sum(TaskTimeEntry.duration_seconds), 0))
            .where(
                TaskTimeEntry.organization_id == self.org_id,
                TaskTimeEntry.task_id.in_(task_ids),
                TaskTimeEntry.stopped_at.is_not(None),
            )
            .group_by(TaskTimeEntry.task_id)
        )
        completed_result = await self.db.execute(completed_stmt)
        completed_by_task: dict[int, int] = {task_id: int(total) for task_id, total in completed_result.all()}

        active_stmt = select(TaskTimeEntry.task_id, TaskTimeEntry.started_at).where(
            TaskTimeEntry.organization_id == self.org_id,
            TaskTimeEntry.task_id.in_(task_ids),
            TaskTimeEntry.stopped_at.is_(None),
        )
        active_result = await self.db.execute(active_stmt)

        active_elapsed_by_task: dict[int, int] = {}
        active_count_by_task: dict[int, int] = {}
        for task_id, started_at in active_result.all():
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=timezone.utc)
            elapsed = max(int((now - started_at).total_seconds()), 0)
            active_elapsed_by_task[task_id] = active_elapsed_by_task.get(task_id, 0) + elapsed
            active_count_by_task[task_id] = active_count_by_task.get(task_id, 0) + 1

        summaries: dict[int, tuple[int, int]] = {}
        for task_id in set(completed_by_task) | set(active_count_by_task):
            working_time_seconds = completed_by_task.get(task_id, 0) + active_elapsed_by_task.get(task_id, 0)
            summaries[task_id] = (working_time_seconds, active_count_by_task.get(task_id, 0))
        return summaries
