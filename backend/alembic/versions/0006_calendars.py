"""calendar feeds — bookings that propose their own turnovers

An owner points us at the .ics their listing already publishes, and each
checkout in it becomes a **draft** turnover they confirm. Not a live job: a feed
is a read-only projection of somebody else's system, and a test booking or a
glitch posting ten real jobs to the bench is not recoverable by the owner, only
apologised for.

Four groups of columns, each with a reason:

* **`property_calendars`** — the feed itself, plus what happened last time it
  ran. `last_error` and `last_booking_count` exist because a sync that quietly
  stops working looks exactly like a calendar with no bookings in it, and the
  difference has to be legible on the owner's screen rather than in a log.

* **`turnovers.source_calendar_id` / `external_ref`** — where a job came from
  and which booking it is, with a unique constraint across the pair. Identity is
  the feed's own event UID rather than the dates, so a booking that moves is
  still one booking; the constraint is what makes re-syncing idempotent instead
  of a way to accumulate a duplicate every fifteen minutes.

* **`turnovers.source_synced_at` / `owner_edited_at`** — the two halves of the
  only question a re-sync needs to ask: has a person touched this since we wrote
  it? If they have, the feed does not get to argue with them. Two columns rather
  than a comparison against `updated_at`, because `updated_at` also moves for
  system maintenance — the read paths persist a standing vacancy's climb up the
  urgency ladder, so viewing a list would otherwise mark a synced draft edited.

* **`properties.default_checkout_time` / `default_checkin_time`** — what an
  all-day calendar cannot tell us. Airbnb and VRBO export whole days: a guest
  leaves "on the 7th" with no hour attached. The urgency ladder is measured in
  hours, so a synced turnover needs a time from somewhere, and the house's own
  policy is the only honest source. Defaults are the ones most listings use.

Every added column is nullable or carries a server default, so this is safe
against a table with rows in it.

Revision ID: 0006_calendars
Revises: 0005_residential
Create Date: 2026-09-14 17:05:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_calendars"
down_revision: str | None = "0005_residential"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "property_calendars",
        # No server default: the id comes from `UUIDPrimaryKeyMixin`, the same
        # as every other table here. A `gen_random_uuid()` default would be one
        # the model does not declare, and `alembic check` fails on exactly that
        # kind of drift — which is how this was caught.
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "property_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column(
            "label", sa.String(length=80), nullable=False, server_default="Calendar"
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_booking_count", sa.Integer(), nullable=True),
        # The warning the scheduled pass would otherwise only ever log: a
        # booking vanished from a job somebody is already on.
        sa.Column("last_stale_kept", sa.Integer(), nullable=True),
        # Optimistic concurrency for overlapping reads of one feed: a reader
        # notes this before fetching and, under the lock, commits only if it is
        # unchanged. An integer rather than a clock, because "has anything
        # happened since I looked?" is not a question about time.
        sa.Column(
            "sync_epoch", sa.Integer(), nullable=False, server_default="0"
        ),
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
        sa.ForeignKeyConstraint(["property_id"], ["properties.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # The same feed twice on one property would double every booking, since
        # identity is (calendar, event) and two rows are two calendars.
        sa.UniqueConstraint(
            "property_id", "url", name="uq_property_calendars_property_url"
        ),
    )
    op.create_index(
        op.f("ix_property_calendars_property_id"), "property_calendars", ["property_id"]
    )

    op.add_column(
        "turnovers",
        sa.Column(
            "source_calendar_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_index(
        op.f("ix_turnovers_source_calendar_id"), "turnovers", ["source_calendar_id"]
    )
    op.create_foreign_key(
        "fk_turnovers_source_calendar_id",
        "turnovers",
        "property_calendars",
        ["source_calendar_id"],
        ["id"],
        # SET NULL rather than CASCADE: removing a feed must not delete the
        # jobs it proposed. A turnover somebody is booked on outlives the
        # calendar that suggested it.
        ondelete="SET NULL",
    )
    op.add_column(
        "turnovers", sa.Column("external_ref", sa.String(length=500), nullable=True)
    )
    op.create_unique_constraint(
        "uq_turnovers_source_event", "turnovers", ["source_calendar_id", "external_ref"]
    )
    op.add_column(
        "turnovers",
        sa.Column("source_synced_at", sa.DateTime(timezone=True), nullable=True),
    )
    # **A separate column from `updated_at` on purpose.** `updated_at` moves on
    # any write, and the read paths write — `refresh_urgency` persists a
    # standing vacancy climbing the urgency ladder. Using it to mean "a person
    # edited this" would let a page view hand a synced draft to nobody: the
    # feed could no longer correct its dates or withdraw it, and nothing would
    # fail to say so.
    op.add_column(
        "turnovers",
        sa.Column("owner_edited_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Which feed proposed this job, as a digest. Removing a calendar nulls
    # `source_calendar_id`, so without this a reconnecting feed cannot tell its
    # own orphans from another feed's that happens to reuse an event id.
    op.add_column(
        "turnovers", sa.Column("source_feed_key", sa.String(length=64), nullable=True)
    )

    op.add_column(
        "properties",
        sa.Column(
            "default_checkout_time",
            sa.Time(),
            nullable=False,
            server_default="11:00:00",
        ),
    )
    op.add_column(
        "properties",
        sa.Column(
            "default_checkin_time", sa.Time(), nullable=False, server_default="16:00:00"
        ),
    )


def downgrade() -> None:
    op.drop_column("properties", "default_checkin_time")
    op.drop_column("properties", "default_checkout_time")

    op.drop_constraint("uq_turnovers_source_event", "turnovers", type_="unique")
    op.drop_column("turnovers", "source_feed_key")
    op.drop_column("turnovers", "owner_edited_at")
    op.drop_column("turnovers", "source_synced_at")
    op.drop_column("turnovers", "external_ref")
    op.drop_constraint("fk_turnovers_source_calendar_id", "turnovers", type_="foreignkey")
    op.drop_index(op.f("ix_turnovers_source_calendar_id"), table_name="turnovers")
    op.drop_column("turnovers", "source_calendar_id")

    # **Not row-safe, and deliberately loud about it.** Dropping the table takes
    # the feeds with it — but the *turnovers* those feeds proposed stay, because
    # they are real jobs somebody may be booked on. They simply lose the record
    # of where they came from, which is the right trade: a job outlives its
    # suggestion.
    op.drop_index(
        op.f("ix_property_calendars_property_id"), table_name="property_calendars"
    )
    op.drop_table("property_calendars")
