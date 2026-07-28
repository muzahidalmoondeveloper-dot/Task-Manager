"""kpi_integrity_and_snooze

Adds: kpis.reference_max, is_snoozed, snoozed_until, created_by_id;
kpi_entries.updated_at + unique (kpi_id, period_type, period_start);
supporting indexes.

Revision ID: d8e5f1a3c7b9
Revises: c9d4e7f2a5b8
Create Date: 2026-07-18 12:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "d8e5f1a3c7b9"
down_revision: Union[str, None] = "c9d4e7f2a5b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("kpis", sa.Column("reference_max", sa.Float(), nullable=True))
    op.add_column("kpis", sa.Column("is_snoozed", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("kpis", sa.Column("snoozed_until", sa.Date(), nullable=True))
    op.add_column("kpis", sa.Column("created_by_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_kpis_created_by_id_users", "kpis", "users", ["created_by_id"], ["id"], ondelete="SET NULL"
    )
    op.create_index("ix_kpis_team_id", "kpis", ["team_id"])
    op.create_index("ix_kpis_rock_id", "kpis", ["rock_id"])

    op.add_column(
        "kpi_entries",
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    )
    op.create_index("ix_kpi_entries_kpi_id", "kpi_entries", ["kpi_id"])

    # Remove duplicate period rows (keep the newest) before adding the constraint.
    op.execute(
        """
        DELETE FROM kpi_entries a
        USING kpi_entries b
        WHERE a.kpi_id = b.kpi_id
          AND a.period_type = b.period_type
          AND a.period_start = b.period_start
          AND a.id < b.id
        """
    )
    op.create_unique_constraint(
        "uq_kpi_entry_period", "kpi_entries", ["kpi_id", "period_type", "period_start"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_kpi_entry_period", "kpi_entries", type_="unique")
    op.drop_index("ix_kpi_entries_kpi_id", table_name="kpi_entries")
    op.drop_column("kpi_entries", "updated_at")

    op.drop_index("ix_kpis_rock_id", table_name="kpis")
    op.drop_index("ix_kpis_team_id", table_name="kpis")
    op.drop_constraint("fk_kpis_created_by_id_users", "kpis", type_="foreignkey")
    op.drop_column("kpis", "created_by_id")
    op.drop_column("kpis", "snoozed_until")
    op.drop_column("kpis", "is_snoozed")
    op.drop_column("kpis", "reference_max")
