"""add_actor_label_to_activity_logs

Revision ID: e5a9c3f7d2b4
Revises: d4f1a8c3b6e7
Create Date: 2026-09-06 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "e5a9c3f7d2b4"
down_revision: Union[str, None] = "d4f1a8c3b6e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "activity_logs",
        sa.Column("actor_label", sa.String(length=255), nullable=True),
    )

    # Backfill: for existing rows whose actor_user_id still points at a
    # live user, snapshot that user's current full_name -- NEVER email.
    # ActivityLog is append-only and long-lived, so anything written into
    # actor_label here is effectively permanent; unlike the *live*-actor
    # display convention used elsewhere in this app (full_name, falling
    # back to email), this immutable historical snapshot must never
    # persist an email address, its local-part, phone, username-as-email,
    # or any other login/contact identifier. A user with no full_name gets
    # the same generic "Unknown User" label the API already falls back to
    # for a nameless live actor (see app/api/routes/activity_logs.py's
    # _serialize) -- never a fabricated name.
    #
    # Rows whose actor_user_id is already NULL (the user was deleted
    # before this migration ever ran) have no recoverable identity at
    # all -- actor_label is deliberately left NULL for those, and the API
    # continues to fall back to "Deleted User" for them. Fabricating a
    # name for these would be worse than admitting the gap.
    op.execute(
        """
        UPDATE activity_logs
        SET actor_label = COALESCE(NULLIF(TRIM(users.full_name), ''), 'Unknown User')
        FROM users
        WHERE activity_logs.actor_user_id = users.id
          AND activity_logs.actor_label IS NULL
        """
    )

    # Corrective pass: this migration's first version (before this privacy
    # fix) backfilled with `COALESCE(full_name, email)`, so any row already
    # touched by that version may have an email address sitting in
    # actor_label. This migration has never been applied via `alembic
    # upgrade` in any tracked environment (no environment for this app
    # tracks alembic_version yet -- see main.py's dual schema-sync notes),
    # so there is no separate corrective migration; the fix is folded into
    # this same still-pending migration. Re-running this UPDATE is safe
    # and idempotent: it only touches rows that still look like an email.
    op.execute(
        """
        UPDATE activity_logs
        SET actor_label = COALESCE(NULLIF(TRIM(users.full_name), ''), 'Unknown User')
        FROM users
        WHERE activity_logs.actor_user_id = users.id
          AND activity_logs.actor_label LIKE '%@%'
        """
    )
    # And for rows whose actor has since been deleted (actor_user_id is
    # already NULL) but whose actor_label was captured back when this
    # migration's first version ran and still contains an email: sanitize
    # to the safe generic fallback rather than preserving the email or
    # fabricating a name.
    op.execute(
        """
        UPDATE activity_logs
        SET actor_label = 'Unknown User'
        WHERE actor_user_id IS NULL
          AND actor_label LIKE '%@%'
        """
    )


def downgrade() -> None:
    op.drop_column("activity_logs", "actor_label")
