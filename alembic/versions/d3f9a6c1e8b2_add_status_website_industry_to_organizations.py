"""add_status_website_industry_to_organizations

Revision ID: d3f9a6c1e8b2
Revises: c7e1a9b2d4f0
Create Date: 2026-06-22 12:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "d3f9a6c1e8b2"
down_revision: Union[str, None] = "c7e1a9b2d4f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("organizations", sa.Column("website", sa.String(length=500), nullable=True))
    op.add_column("organizations", sa.Column("industry", sa.String(length=100), nullable=True))
    op.add_column(
        "organizations",
        sa.Column("status", sa.String(length=30), nullable=False, server_default="active"),
    )
    op.create_index("ix_organizations_status", "organizations", ["status"])


def downgrade() -> None:
    op.drop_index("ix_organizations_status", table_name="organizations")
    op.drop_column("organizations", "status")
    op.drop_column("organizations", "industry")
    op.drop_column("organizations", "website")
