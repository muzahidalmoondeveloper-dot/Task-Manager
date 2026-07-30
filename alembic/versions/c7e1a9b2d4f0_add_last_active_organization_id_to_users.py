"""add_last_active_organization_id_to_users

Revision ID: c7e1a9b2d4f0
Revises: f87667d37eff
Create Date: 2026-06-15 12:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c7e1a9b2d4f0"
down_revision: Union[str, None] = "f87667d37eff"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "last_active_organization_id",
            sa.Uuid(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_users_last_active_organization_id",
        "users",
        ["last_active_organization_id"],
    )
    op.create_foreign_key(
        "fk_users_last_active_organization_id",
        "users",
        "organizations",
        ["last_active_organization_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_users_last_active_organization_id", "users", type_="foreignkey")
    op.drop_index("ix_users_last_active_organization_id", table_name="users")
    op.drop_column("users", "last_active_organization_id")
