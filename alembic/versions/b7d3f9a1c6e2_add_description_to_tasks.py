"""add_description_to_tasks

Revision ID: b7d3f9a1c6e2
Revises: a4f8c2e6b1d9
Create Date: 2026-09-06 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "b7d3f9a1c6e2"
down_revision: Union[str, None] = "a4f8c2e6b1d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("description", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tasks", "description")
