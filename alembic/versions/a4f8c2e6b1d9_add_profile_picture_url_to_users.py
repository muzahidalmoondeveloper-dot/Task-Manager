"""add_profile_picture_url_to_users

Revision ID: a4f8c2e6b1d9
Revises: e6a2b8d4f1c3
Create Date: 2026-09-06 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a4f8c2e6b1d9"
down_revision: Union[str, None] = "e6a2b8d4f1c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("profile_picture_url", sa.String(length=500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "profile_picture_url")
