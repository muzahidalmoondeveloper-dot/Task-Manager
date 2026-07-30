"""add_team_news_links

Revision ID: e7c2b5a9f1d4
Revises: d3f9a6c1e8b2
Create Date: 2026-06-30 09:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "e7c2b5a9f1d4"
down_revision: Union[str, None] = "d3f9a6c1e8b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "team_news_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "news_id",
            sa.Integer(),
            sa.ForeignKey("team_news.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("linked_type", sa.String(length=20), nullable=False),
        sa.Column("linked_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("news_id", "linked_type", "linked_id", name="uq_team_news_link"),
    )
    op.create_index("ix_team_news_links_id", "team_news_links", ["id"])
    op.create_index("ix_team_news_links_news_id", "team_news_links", ["news_id"])


def downgrade() -> None:
    op.drop_index("ix_team_news_links_news_id", table_name="team_news_links")
    op.drop_index("ix_team_news_links_id", table_name="team_news_links")
    op.drop_table("team_news_links")
