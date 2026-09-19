"""Removal archives a calendar instead of deleting it.

A feed's identity is not observable from outside — the export URL rotates and
the event UIDs are arbitrary feed-local strings — so the only stable thing a
job could be keyed to was its calendar's row id, and removal threw that away.
`turnovers.source_feed_key` was the last of three attempts to reconstruct an
identity after destroying it, and like the two before it, it had an edge: the
provider rotates the export URL, the owner has to remove and re-add, the new
digest matches nothing, and every booking duplicates.

Keeping the row removes the problem rather than guarding against it, which is
why this migration is mostly a deletion.

**`source_feed_key` is dropped rather than left in place.** A nullable column
nothing reads is not free: the next person to open the model has to work out
whether it is load-bearing, and the honest answer — "it reconstructs an
identity we no longer destroy" — is only obvious if you know the history. The
downgrade recreates it empty, and adoption is gone, so a downgrade cannot
restore the old behaviour. That is the right trade: this is a forward fix, and
pretending otherwise would mean carrying dead reconstruction machinery for a
rollback nobody is going to run.

Revision ID: 0011_archive_calendars
Revises: 0010_notification_sent_via
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_archive_calendars"
down_revision = "0010_notification_sent_via"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "property_calendars",
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_property_calendars_removed_at",
        "property_calendars",
        ["removed_at"],
    )
    # Every existing row is live: removal has always meant DELETE until now, so
    # there is nothing archived to backfill and no guess being made here.
    op.drop_column("turnovers", "source_feed_key")


def downgrade() -> None:
    op.add_column(
        "turnovers",
        sa.Column("source_feed_key", sa.String(length=64), nullable=True),
    )
    op.drop_index("ix_property_calendars_removed_at", table_name="property_calendars")
    op.drop_column("property_calendars", "removed_at")
