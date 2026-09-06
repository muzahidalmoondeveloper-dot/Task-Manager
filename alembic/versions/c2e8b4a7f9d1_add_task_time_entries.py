"""add_task_time_entries

Revision ID: c2e8b4a7f9d1
Revises: b7d3f9a1c6e2
Create Date: 2026-09-06 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c2e8b4a7f9d1"
down_revision: Union[str, None] = "b7d3f9a1c6e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "task_time_entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("organization_id", sa.Uuid(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_task_time_entries_id", "task_time_entries", ["id"])
    op.create_index("ix_task_time_entries_task_id", "task_time_entries", ["task_id"])
    op.create_index("ix_task_time_entries_user_id", "task_time_entries", ["user_id"])
    op.create_index("ix_task_time_entries_organization_id", "task_time_entries", ["organization_id"])

    # Enforces "at most one active timer per user" at the database level
    # (Phase 4/18) — a partial unique index over user_id, restricted to
    # rows where stopped_at IS NULL. Two concurrent Start requests for the
    # same user race to insert; only one succeeds, the other hits a unique
    # violation the API translates into a clean 409 conflict, instead of
    # both silently creating an active session (which would double-count
    # working time). PostgreSQL-specific (partial index) — this project's
    # dev/test/production database is PostgreSQL throughout (see
    # DATABASE_URL / AsyncSessionLocal usage across the existing test
    # suite), so no SQLite fallback path is needed here.
    op.create_index(
        "ix_task_time_entries_one_active_per_user",
        "task_time_entries",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("stopped_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_task_time_entries_one_active_per_user", table_name="task_time_entries")
    op.drop_index("ix_task_time_entries_organization_id", table_name="task_time_entries")
    op.drop_index("ix_task_time_entries_user_id", table_name="task_time_entries")
    op.drop_index("ix_task_time_entries_task_id", table_name="task_time_entries")
    op.drop_index("ix_task_time_entries_id", table_name="task_time_entries")
    op.drop_table("task_time_entries")
