"""stripe connect — payout accounts, checkout sessions, and job completion

Phase 6. No new tables: `payments_in` and `payouts` were built in phase 1 and
have been sitting empty on purpose. This adds the columns the live money path
needs on top of them, plus the two fields that record a job actually being done.

Three groups, each with a reason:

* **`cleaner_profiles.stripe_*`** — the Express connected account, and Stripe's
  own answer about whether money can reach it. Deliberately *not* folded into
  `can_take_jobs`, which stays a generated column over the two vetting statuses
  with exactly one author. Being trusted in a stranger's house and being able to
  receive a transfer are different questions, and a Stripe verification delay
  must not silently stop a vetted cleaner from bidding.

* **`payments_in.stripe_checkout_session_id`** — the owner enters card details
  on Stripe's hosted page, never on ours. `payouts.reversed_amount_cents` is its
  counterpart on the refund side: a reversed transfer is recorded, never
  deleted, the same reason a cancelled award is kept as history.

* **`awards.started_at` / `completed_at`** — money hangs off the second one. The
  owner is charged for a finished job rather than a booked one, so an ordinary
  cancellation needs no refund at all and the refund path stays reserved for
  something going wrong after the work was done.

One enum value is added with it: `notification_event.job_completed`. The fixed
list was closed at twelve, and this is the thirteenth, added deliberately
because the transition it belongs to is the one the owner now has to act on —
nobody hearing "the cleaner says it is done" means nobody pays. Postgres cannot
remove an enum value, so the downgrade rebuilds the type without it rather than
leaving a value behind that a later upgrade would collide with.

Every added column is nullable or carries a server default, so the migration is
safe against a table with rows in it — there are none in production yet, and
writing it as though there were costs nothing.

Revision ID: 0004_stripe_connect
Revises: 0003_notifications
Create Date: 2026-09-14 03:10:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_stripe_connect"
down_revision: str | None = "0003_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- the cleaner's payout account -------------------------------------
    op.add_column(
        "cleaner_profiles",
        sa.Column("stripe_account_id", sa.String(length=255), nullable=True),
    )
    op.create_unique_constraint(
        op.f("uq_cleaner_profiles_stripe_account_id"),
        "cleaner_profiles",
        ["stripe_account_id"],
    )
    op.add_column(
        "cleaner_profiles",
        sa.Column(
            "stripe_payouts_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "cleaner_profiles",
        sa.Column(
            "stripe_details_submitted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # --- hosted checkout, and the refund side of a payout -----------------
    op.add_column(
        "payments_in",
        sa.Column("stripe_checkout_session_id", sa.String(length=255), nullable=True),
    )
    op.create_unique_constraint(
        op.f("uq_payments_in_stripe_checkout_session_id"),
        "payments_in",
        ["stripe_checkout_session_id"],
    )
    op.add_column(
        "payouts",
        sa.Column(
            "reversed_amount_cents",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.create_check_constraint(
        "reversal_within_amount",
        "payouts",
        "reversed_amount_cents >= 0 AND reversed_amount_cents <= amount_cents",
    )

    # --- the thirteenth event ---------------------------------------------
    # Added, not invented: the completion transition below is one the owner has
    # to act on, and an event nobody is told about is a job nobody pays for.
    op.execute("ALTER TYPE notification_event ADD VALUE IF NOT EXISTS 'job_completed'")

    # --- the job actually being done --------------------------------------
    op.add_column(
        "awards", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "awards", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True)
    )


#: The fixed list as it stood before this migration. Kept here as a literal,
#: the way 0001 keeps a copy of the can_take_jobs expression: a migration is a
#: historical snapshot and must not read today's enum to describe yesterday's.
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
    "payment_receipt",
    "payout_notice",
    "review_received",
)


def downgrade() -> None:
    # Postgres has no DROP VALUE. Rebuilding the type is the only honest way
    # back, and doing it means a downgrade followed by an upgrade works twice
    # rather than failing the second time on a value that never went away.
    op.execute("ALTER TYPE notification_event RENAME TO notification_event_old")
    values = ", ".join(f"'{value}'" for value in NOTIFICATION_EVENTS_BEFORE)
    op.execute(f"CREATE TYPE notification_event AS ENUM ({values})")
    op.execute(
        "ALTER TABLE notifications ALTER COLUMN event TYPE notification_event "
        "USING event::text::notification_event"
    )
    op.execute("DROP TYPE notification_event_old")

    op.drop_column("awards", "completed_at")
    op.drop_column("awards", "started_at")

    op.drop_constraint("reversal_within_amount", "payouts", type_="check")
    op.drop_column("payouts", "reversed_amount_cents")
    op.drop_constraint(
        op.f("uq_payments_in_stripe_checkout_session_id"), "payments_in", type_="unique"
    )
    op.drop_column("payments_in", "stripe_checkout_session_id")

    op.drop_column("cleaner_profiles", "stripe_details_submitted")
    op.drop_column("cleaner_profiles", "stripe_payouts_enabled")
    op.drop_constraint(
        op.f("uq_cleaner_profiles_stripe_account_id"), "cleaner_profiles", type_="unique"
    )
    op.drop_column("cleaner_profiles", "stripe_account_id")
