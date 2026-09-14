"""disputes — the human inbox, made real

Phase 8. The one new table in the product since phase 1, and it is new for a
reason worth writing down: phase 1 built a table for everything the plan called
for, and disputes were not among them because "disputes go to a human inbox, not
a bot, at v1" had been read as *there is nothing to build*. There was something
to build — a way to raise one. Without it, "a human handles it" means "somebody
works out how to email us", and the complaints that never arrive are exactly the
ones worth having.

Two new enum types come with it (`dispute_status`, `dispute_reason`), and two
values are added to `notification_event`, taking the fixed list from thirteen to
fifteen. Opening a closed list is meant to be a decision rather than a habit, so
the reasoning is here as well as in `app/models/enums.py`:

* `dispute_raised` — an admin, **and the person who raised it**. Somebody who
  reports that a stranger was in their house and hears nothing assumes it went
  nowhere. The other party is deliberately not told at this point; a human
  decides when to involve somebody in a complaint about them, which is the whole
  point of the inbox being a person.
* `dispute_resolved` — both parties, carrying the admin's note. It is the only
  place either side learns what was decided.

Postgres cannot drop an enum value, so the downgrade rebuilds `notification_event`
without the two, and refuses rather than discarding rows it cannot represent —
the same shape as 0004, for the same reason: those rows are the record that
somebody was told.

Revision ID: 0005_disputes
Revises: 0004_stripe_connect
Create Date: 2026-09-14 15:20:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_disputes"
down_revision: str | None = "0004_stripe_connect"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: The new enums, as literals. A migration is a historical snapshot and must not
#: read today's Python enum to describe the shape it created — the same rule
#: 0001 follows for the `can_take_jobs` expression.
DISPUTE_STATUS = ("open", "acknowledged", "resolved")
DISPUTE_REASON = ("quality", "access", "damage", "payment", "conduct", "other")

#: The two events this migration adds.
NEW_EVENTS = ("dispute_raised", "dispute_resolved")

#: The fixed list as it stood before this migration — thirteen, after phase 6
#: added `job_completed`.
NOTIFICATION_EVENTS_BEFORE = (
    "turnover_posted",
    "bid_received",
    "bid_accepted",
    "bid_declined",
    "turnover_reminder",
    "cleaner_cancelled",
    "cleaner_no_show",
    "owner_cancelled_awarded",
    "turnover_unclaimed",
    "job_completed",
    "payment_receipt",
    "payout_notice",
    "review_received",
)


def upgrade() -> None:
    # Create the two new types up front, then reference them with
    # `create_type=False` below. Letting `create_table` create them implicitly
    # is how a migration ends up trying to CREATE TYPE twice — and `user_role`,
    # which already exists from 0001, must never be created here at all.
    postgresql.ENUM(*DISPUTE_STATUS, name="dispute_status").create(
        op.get_bind(), checkfirst=True
    )
    postgresql.ENUM(*DISPUTE_REASON, name="dispute_reason").create(
        op.get_bind(), checkfirst=True
    )
    dispute_status = postgresql.ENUM(
        *DISPUTE_STATUS, name="dispute_status", create_type=False
    )
    dispute_reason = postgresql.ENUM(
        *DISPUTE_REASON, name="dispute_reason", create_type=False
    )
    user_role = postgresql.ENUM(
        "owner", "cleaner", "admin", name="user_role", create_type=False
    )

    op.create_table(
        "disputes",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        # Keyed to the turnover rather than the award: a dispute about a job
        # whose award was cancelled is still a dispute, and arguably the kind
        # most worth reading.
        sa.Column(
            "turnover_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "raised_by_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False
        ),
        # Which side they were on when they raised it, stored rather than
        # re-derived: an award can be cancelled afterwards, and "who was
        # complaining" must not change its answer later.
        sa.Column("raised_by_role", user_role, nullable=False),
        sa.Column("reason", dispute_reason, nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", dispute_status, nullable=False, server_default="open"),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "acknowledged_by_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "resolved_by_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("resolution_notes", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(["turnover_id"], ["turnovers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["raised_by_id"], ["users.id"], ondelete="CASCADE"),
        # SET NULL rather than CASCADE: an admin account going away must not
        # delete the record of what they decided.
        sa.ForeignKeyConstraint(
            ["acknowledged_by_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["resolved_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_disputes_turnover_id"), "disputes", ["turnover_id"])
    op.create_index(op.f("ix_disputes_raised_by_id"), "disputes", ["raised_by_id"])
    # The inbox reads open ones oldest-first. Oldest-first on an unbounded queue
    # is what stops a complaint being buried under newer noise.
    op.create_index(
        "ix_disputes_status_created_at", "disputes", ["status", "created_at"]
    )

    for event in NEW_EVENTS:
        op.execute(f"ALTER TYPE notification_event ADD VALUE IF NOT EXISTS '{event}'")


def downgrade() -> None:
    # Same shape as 0004's downgrade, and for the same reason. Postgres has no
    # DROP VALUE, so the type is rebuilt without the two new events — and rows
    # carrying them have no representation in the thirteen-value type. Those
    # rows are the record that somebody was told about a complaint, so this
    # refuses and names them rather than deleting them quietly.
    bind = op.get_bind()
    listed = ", ".join(f"'{event}'" for event in NEW_EVENTS)
    stranded = bind.execute(
        sa.text(f"SELECT count(*) FROM notifications WHERE event IN ({listed})")
    ).scalar_one()
    if stranded:
        raise RuntimeError(
            f"{stranded} dispute notification(s) cannot be represented in the "
            "pre-phase-8 enum. Decide what happens to them — archive or delete "
            "the rows — before downgrading; this migration will not discard "
            "them for you."
        )

    # The table goes whole, so its rows are not a separate question: dropping
    # `disputes` is the downgrade. Anyone running it on a database with real
    # complaints in it is discarding them on purpose.
    op.drop_index("ix_disputes_status_created_at", table_name="disputes")
    op.drop_index(op.f("ix_disputes_raised_by_id"), table_name="disputes")
    op.drop_index(op.f("ix_disputes_turnover_id"), table_name="disputes")
    op.drop_table("disputes")

    postgresql.ENUM(name="dispute_reason").drop(bind, checkfirst=True)
    postgresql.ENUM(name="dispute_status").drop(bind, checkfirst=True)

    op.execute("ALTER TYPE notification_event RENAME TO notification_event_old")
    values = ", ".join(f"'{value}'" for value in NOTIFICATION_EVENTS_BEFORE)
    op.execute(f"CREATE TYPE notification_event AS ENUM ({values})")
    op.execute(
        "ALTER TABLE notifications ALTER COLUMN event TYPE notification_event "
        "USING event::text::notification_event"
    )
    op.execute("DROP TYPE notification_event_old")
