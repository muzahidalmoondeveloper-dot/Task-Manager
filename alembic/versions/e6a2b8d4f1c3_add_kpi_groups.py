"""add_kpi_groups

Creates the kpi_groups table, links kpis.kpi_group_id to it, and backfills
groups from the legacy free-text kpis.kpi_group labels (per team).

Revision ID: e6a2b8d4f1c3
Revises: d8e5f1a3c7b9
Create Date: 2026-07-18 15:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "e6a2b8d4f1c3"
down_revision: Union[str, None] = "d8e5f1a3c7b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "kpi_groups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("teams.id", ondelete="CASCADE"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True),
        sa.Column("formula", sa.String(length=20), nullable=False, server_default="sum"),
        sa.Column("collapse_by_default", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("team_id", "name", name="uq_kpi_group_team_name"),
    )
    op.create_index("ix_kpi_groups_id", "kpi_groups", ["id"])
    op.create_index("ix_kpi_groups_team_id", "kpi_groups", ["team_id"])
    op.create_index("ix_kpi_groups_organization_id", "kpi_groups", ["organization_id"])

    op.add_column("kpis", sa.Column("kpi_group_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_kpis_kpi_group_id", "kpis", "kpi_groups", ["kpi_group_id"], ["id"], ondelete="SET NULL"
    )
    op.create_index("ix_kpis_kpi_group_id", "kpis", ["kpi_group_id"])

    # Backfill: one group per distinct (team, legacy label), then link KPIs.
    op.execute(
        """
        INSERT INTO kpi_groups (name, team_id, organization_id, formula, collapse_by_default)
        SELECT DISTINCT trim(kpi_group), team_id, organization_id, 'sum', FALSE
        FROM kpis
        WHERE kpi_group IS NOT NULL AND trim(kpi_group) <> ''
        """
    )
    op.execute(
        """
        UPDATE kpis k
        SET kpi_group_id = g.id
        FROM kpi_groups g
        WHERE k.kpi_group IS NOT NULL
          AND trim(k.kpi_group) = g.name
          AND k.team_id = g.team_id
        """
    )


def downgrade() -> None:
    op.drop_index("ix_kpis_kpi_group_id", table_name="kpis")
    op.drop_constraint("fk_kpis_kpi_group_id", "kpis", type_="foreignkey")
    op.drop_column("kpis", "kpi_group_id")
    op.drop_index("ix_kpi_groups_organization_id", table_name="kpi_groups")
    op.drop_index("ix_kpi_groups_team_id", table_name="kpi_groups")
    op.drop_index("ix_kpi_groups_id", table_name="kpi_groups")
    op.drop_table("kpi_groups")
