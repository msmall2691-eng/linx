"""Owner and booked cleaner can talk to each other.

In-app messaging was listed as out of scope for v1, on the reasoning that email
and SMS are enough at this size. That is true of *notifications* — a one-way
alert does not need a thread — and false of the question a cleaner standing at
a gate actually has, which previously went to a phone number this product
deliberately does not hand out, or went unasked.

The table hangs off the **award** rather than the turnover, because a turnover
can be booked twice — a cleaner backs out and it is re-awarded — and one thread
spanning both would hand the replacement a conversation they were never part
of. `ON DELETE CASCADE` follows the award for the same reason the rest of this
schema cascades from the thing that owns the row.

Seventeenth notification event with it: a message nobody is told about is a
message nobody reads, and there is no push channel in this product.

Revision ID: 0013_job_messages
Revises: 0012_en_route
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_job_messages"
down_revision = "0012_en_route"
branch_labels = None
depends_on = None

NEW_EVENTS = ("message_received",)


def upgrade() -> None:
    op.create_table(
        "job_messages",
        sa.Column("id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("award_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("sender_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["award_id"],
            ["awards.id"],
            name=op.f("fk_job_messages_award_id_awards"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["sender_id"],
            ["users.id"],
            name=op.f("fk_job_messages_sender_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_messages")),
    )
    op.create_index(op.f("ix_job_messages_award_id"), "job_messages", ["award_id"])
    op.create_index(op.f("ix_job_messages_sender_id"), "job_messages", ["sender_id"])
    # The only query there is: one thread, oldest first.
    op.create_index(
        "ix_job_messages_award_created_at", "job_messages", ["award_id", "created_at"]
    )

    for value in NEW_EVENTS:
        op.execute(f"ALTER TYPE notification_event ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    op.drop_index("ix_job_messages_award_created_at", table_name="job_messages")
    op.drop_index(op.f("ix_job_messages_sender_id"), table_name="job_messages")
    op.drop_index(op.f("ix_job_messages_award_id"), table_name="job_messages")
    op.drop_table("job_messages")

    # **Every value 0012 had, and only the one this revision added is dropped.**
    # Postgres cannot remove a value from an enum, so the type is rebuilt — and
    # it has to be rebuilt as *the parent revision's* enum, or this leaves
    # behind a schema the project never had.
    remaining = [
        "turnover_posted", "bid_received", "bid_accepted", "bid_declined",
        "turnover_reminder", "cleaner_cancelled", "cleaner_no_show",
        "owner_cancelled_awarded", "turnover_unclaimed", "job_completed",
        "payment_receipt", "payout_notice", "review_received",
        "dispute_raised", "dispute_resolved", "cleaner_en_route",
    ]
    values = ", ".join(f"'{v}'" for v in remaining)
    op.execute("ALTER TYPE notification_event RENAME TO notification_event_old")
    op.execute(f"CREATE TYPE notification_event AS ENUM ({values})")
    op.execute(
        "ALTER TABLE notifications ALTER COLUMN event TYPE notification_event "
        "USING event::text::notification_event"
    )
    op.execute("DROP TYPE notification_event_old")
