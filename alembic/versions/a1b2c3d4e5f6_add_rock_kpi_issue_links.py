"""add_rock_kpi_issue_links

Revision ID: a1b2c3d4e5f6
Revises: e7c2b5a9f1d4
Create Date: 2026-07-01 09:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "e7c2b5a9f1d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "rock_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("rock_id", sa.Integer(), sa.ForeignKey("rocks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("linked_type", sa.String(length=20), nullable=False),
        sa.Column("linked_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("rock_id", "linked_type", "linked_id", name="uq_rock_link"),
    )
    op.create_index("ix_rock_links_id", "rock_links", ["id"])
    op.create_index("ix_rock_links_rock_id", "rock_links", ["rock_id"])

    op.create_table(
        "kpi_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kpi_id", sa.Integer(), sa.ForeignKey("kpis.id", ondelete="CASCADE"), nullable=False),
        sa.Column("linked_type", sa.String(length=20), nullable=False),
        sa.Column("linked_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("kpi_id", "linked_type", "linked_id", name="uq_kpi_link"),
    )
    op.create_index("ix_kpi_links_id", "kpi_links", ["id"])
    op.create_index("ix_kpi_links_kpi_id", "kpi_links", ["kpi_id"])

    op.create_table(
        "issue_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("issue_id", sa.Integer(), sa.ForeignKey("issues.id", ondelete="CASCADE"), nullable=False),
        sa.Column("linked_type", sa.String(length=20), nullable=False),
        sa.Column("linked_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("issue_id", "linked_type", "linked_id", name="uq_issue_link"),
    )
    op.create_index("ix_issue_links_id", "issue_links", ["id"])
    op.create_index("ix_issue_links_issue_id", "issue_links", ["issue_id"])


def downgrade() -> None:
    op.drop_index("ix_issue_links_issue_id", table_name="issue_links")
    op.drop_index("ix_issue_links_id", table_name="issue_links")
    op.drop_table("issue_links")

    op.drop_index("ix_kpi_links_kpi_id", table_name="kpi_links")
    op.drop_index("ix_kpi_links_id", table_name="kpi_links")
    op.drop_table("kpi_links")

    op.drop_index("ix_rock_links_rock_id", table_name="rock_links")
    op.drop_index("ix_rock_links_id", table_name="rock_links")
    op.drop_table("rock_links")
