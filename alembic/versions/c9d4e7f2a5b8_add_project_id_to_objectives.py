"""add_project_id_to_objectives

Revision ID: c9d4e7f2a5b8
Revises: b2c3d4e5f6a7
Create Date: 2026-07-18 09:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c9d4e7f2a5b8"
down_revision: Union[str, None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("objectives", sa.Column("project_id", sa.Integer(), nullable=True))
    op.create_index("ix_objectives_project_id", "objectives", ["project_id"])
    op.create_foreign_key(
        "fk_objectives_project_id_projects",
        "objectives",
        "projects",
        ["project_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_objectives_project_id_projects", "objectives", type_="foreignkey")
    op.drop_index("ix_objectives_project_id", table_name="objectives")
    op.drop_column("objectives", "project_id")
