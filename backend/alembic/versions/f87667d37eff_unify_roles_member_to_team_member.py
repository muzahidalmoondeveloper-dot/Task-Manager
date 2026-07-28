"""unify_roles_member_to_team_member

Revision ID: f87667d37eff
Revises: 8f3a4e4936ab
Create Date: 2026-06-10 11:50:44.504580
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f87667d37eff'
down_revision: Union[str, None] = '8f3a4e4936ab'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE organization_memberships SET role='team_member' WHERE role='member'")
    op.execute("UPDATE organization_invitations SET role='team_member' WHERE role='member'")


def downgrade() -> None:
    op.execute("UPDATE organization_memberships SET role='member' WHERE role='team_member'")
    op.execute("UPDATE organization_invitations SET role='member' WHERE role='team_member'")