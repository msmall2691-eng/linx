"""disputes — a complaint with a person on the other end

Phase 8's one new table, plus the two notification events that make it real.

**A dispute is a disagreement a human decides**, not a request the system
approves. That is why `dispute_status` has three values and no `rejected`: the
outcome lives in `resolution_notes` as prose somebody wrote, because at this
size the outcomes are not an enumerable set and pretending otherwise puts a
policy in a column.

Three deletion rules, each chosen rather than defaulted:

* **`turnover_id` cascades.** A dispute is about a specific job; if the job is
  gone the complaint has no subject.
* **`raised_by_id` cascades.** A deleted account's complaint has nobody to
  resolve it with.
* **`acknowledged_by_id` and `resolved_by_id` are `SET NULL`.** An admin
  leaving must not erase the record that somebody handled it. *Who* decided
  becomes unknown; *that* it was decided, and what was decided, survives —
  which is the half a dispute is argued from later.

The `(status, created_at)` index is the inbox's own query: open ones first,
oldest first, which is the order a person works a queue in.

Revision ID: 0007_disputes
Revises: 0006_calendars
Create Date: 2026-09-14 21:30:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_disputes"
down_revision: str | None = "0006_calendars"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The two events phase 8 adds. **Alembic's autogenerate cannot see these** —
#: it diffs tables and columns, not the values inside a native enum type — so
#: an added `NotificationEvent` member is a migration somebody writes by hand
#: or a deploy that fails on the first row. Named here so the list and the type
#: are changed in one place.
NEW_EVENTS = ("dispute_raised", "dispute_resolved")


def upgrade() -> None:
    # Created up front and referenced with `create_type=False` below — the
    # idiom `0001_initial_schema` established, because letting the column
    # definition create the type makes the order of columns load-bearing.
    bind = op.get_bind()
    postgresql.ENUM(
        "quality", "access", "damage", "payment", "conduct", "other",
        name="dispute_reason",
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        "open", "acknowledged", "resolved", name="dispute_status"
    ).create(bind, checkfirst=True)

    op.create_table(
        "disputes",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "turnover_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "raised_by_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False
        ),
        # Stored rather than read back through the user, because a role can
        # change and "who was complaining, as what" is the historical fact a
        # dispute is read with.
        sa.Column(
            "raised_by_role",
            postgresql.ENUM(
                "owner", "cleaner", "admin", name="user_role", create_type=False
            ),
            nullable=False,
        ),
        sa.Column(
            "reason",
            postgresql.ENUM(
                "quality", "access", "damage", "payment", "conduct", "other",
                name="dispute_reason", create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "open", "acknowledged", "resolved",
                name="dispute_status", create_type=False,
            ),
            nullable=False,
            server_default="open",
        ),
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
        sa.ForeignKeyConstraint(
            ["turnover_id"], ["turnovers.id"],
            name=op.f("fk_disputes_turnover_id_turnovers"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["raised_by_id"], ["users.id"],
            name=op.f("fk_disputes_raised_by_id_users"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["acknowledged_by_id"], ["users.id"],
            name=op.f("fk_disputes_acknowledged_by_id_users"), ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by_id"], ["users.id"],
            name=op.f("fk_disputes_resolved_by_id_users"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_disputes")),
    )
    op.create_index(op.f("ix_disputes_turnover_id"), "disputes", ["turnover_id"])
    op.create_index(op.f("ix_disputes_raised_by_id"), "disputes", ["raised_by_id"])
    # The inbox's own query: open first, oldest first.
    op.create_index("ix_disputes_status_created_at", "disputes", ["status", "created_at"])

    # `ALTER TYPE ... ADD VALUE` cannot run inside a transaction block on older
    # servers and cannot be undone at all, which is why the downgrade below
    # rebuilds the type instead of trying to remove a value.
    for value in NEW_EVENTS:
        op.execute(f"ALTER TYPE notification_event ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    op.drop_index("ix_disputes_status_created_at", table_name="disputes")
    op.drop_index(op.f("ix_disputes_raised_by_id"), table_name="disputes")
    op.drop_index(op.f("ix_disputes_turnover_id"), table_name="disputes")
    op.drop_table("disputes")

    postgresql.ENUM(name="dispute_status").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="dispute_reason").drop(op.get_bind(), checkfirst=True)

    # **Postgres cannot remove a value from an enum**, so the type is rebuilt
    # without the two. Any row still carrying one would fail the cast, which is
    # the right outcome: a notification that cannot be described is not one to
    # silently discard.
    remaining = [
        "turnover_posted", "bid_received", "bid_accepted", "bid_declined",
        "turnover_reminder", "cleaner_cancelled", "turnover_unclaimed",
        "job_completed", "payment_receipt", "payout_notice", "review_received",
    ]
    values = ", ".join(f"'{v}'" for v in remaining)
    op.execute("ALTER TYPE notification_event RENAME TO notification_event_old")
    op.execute(f"CREATE TYPE notification_event AS ENUM ({values})")
    op.execute(
        "ALTER TABLE notifications ALTER COLUMN event TYPE notification_event "
        "USING event::text::notification_event"
    )
    op.execute("DROP TYPE notification_event_old")
