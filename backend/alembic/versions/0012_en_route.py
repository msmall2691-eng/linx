"""A cleaner can say they are on the way.

`started_at` already answered "did anybody turn up", after the fact. It could
not answer the question an owner actually asks on the morning of a turnover,
which is *is somebody coming* — and a cleaner who is stuck in traffic had no way
to say so short of the phone call this product exists to avoid.

One nullable timestamp and one notification event. The event is the part that
needed a decision rather than a column: `NotificationEvent` is a closed list, so
opening it is deliberate. This is the sixteenth, and it earns its place by the
same test as `job_completed` did — an owner who is never told has no way to find
out, and the failure is silent on both sides.

**No coordinates, here or anywhere.** What is stored is a timestamp the cleaner
wrote by pressing a button. Continuous location on an independent contractor is
a different product with its own consent, retention and disclosure questions,
and a column added quietly is how it would arrive without any of them being
asked.

Revision ID: 0012_en_route
Revises: 0011_archive_calendars
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_en_route"
down_revision = "0011_archive_calendars"
branch_labels = None
depends_on = None

NEW_EVENTS = ("cleaner_en_route",)


def upgrade() -> None:
    op.add_column(
        "awards",
        sa.Column("en_route_at", sa.DateTime(timezone=True), nullable=True),
    )
    # `ALTER TYPE ... ADD VALUE` cannot be undone, which is why the downgrade
    # rebuilds the type rather than trying to remove a value.
    for value in NEW_EVENTS:
        op.execute(f"ALTER TYPE notification_event ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    op.drop_column("awards", "en_route_at")

    # **Every value 0011 had, and only the one this revision added is dropped.**
    # A rebuilt enum has to be the parent revision's enum: anything else is a
    # schema this project never had, and any row carrying a missing value fails
    # the cast — which is the right outcome, since a notification that cannot be
    # described is not one to discard silently.
    remaining = [
        "turnover_posted", "bid_received", "bid_accepted", "bid_declined",
        "turnover_reminder", "cleaner_cancelled", "cleaner_no_show",
        "owner_cancelled_awarded", "turnover_unclaimed", "job_completed",
        "payment_receipt", "payout_notice", "review_received",
        "dispute_raised", "dispute_resolved",
    ]
    values = ", ".join(f"'{v}'" for v in remaining)
    op.execute("ALTER TYPE notification_event RENAME TO notification_event_old")
    op.execute(f"CREATE TYPE notification_event AS ENUM ({values})")
    op.execute(
        "ALTER TABLE notifications ALTER COLUMN event TYPE notification_event "
        "USING event::text::notification_event"
    )
    op.execute("DROP TYPE notification_event_old")
