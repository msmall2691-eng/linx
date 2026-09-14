"""residential — a second kind of property, and a scope of work

**This migration opens something CLAUDE.md explicitly closed.** "Non-STR
recurring residential cleaning" was on the v1 out-of-scope list from day one,
and it was there for a reason worth restating: a short-term rental's clean is
defined by the gap between one guest leaving and the next arriving, and that
window is what the entire urgency ladder measures. A home has no such window.

The scope was reopened deliberately, by the person whose product it is. What
makes it cheap rather than a rewrite is that the model already had the shape:
a turnover with `checkin_at IS NULL` is a standing vacancy, and its urgency is
already measured as *time until the job* rather than the length of a window.
That is exactly what a scheduled house clean is. So residential does not need a
second ladder or a second table — it needs a way to say which kind of place this
is, and what sort of clean was asked for.

Three columns and three enum types:

* **`properties.property_type`** — short-term rental or residential. Decides
  which fields the posting form asks about and how the job reads to a cleaner.
  Defaults to `short_term_rental`, which is what every existing row is.

* **`properties.square_feet`** — optional on purpose. Plenty of owners do not
  know it, and a required field somebody has to guess at produces a number
  worse than no number. Cleaners price on it when it is there.

* **`turnovers.service_type`** — turnover, standard, deep, or move-out. A
  turnover and a move-out are both "a clean" and are not the same job; a
  cleaner who cannot tell them apart before bidding prices one of them wrong,
  and the ones who guess wrong stop bidding. Defaults to `turnover`, which is
  what every existing row is.

`recurrence` is created here as a type but nothing uses a column of it yet:
repeating schedules are their own change, and creating the type with its
siblings keeps the enum set in one migration rather than two.

Every column is nullable or carries a server default, so this is safe against a
table with rows in it.

Revision ID: 0005_residential
Revises: 0004_stripe_connect
Create Date: 2026-09-14 16:05:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_residential"
down_revision: str | None = "0004_stripe_connect"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: The enum values as literals. A migration is a historical snapshot and must
#: not read today's Python enum to describe the shape it created.
PROPERTY_TYPE = ("short_term_rental", "residential")
SERVICE_TYPE = ("turnover", "standard", "deep", "move_out")
RECURRENCE = ("once", "weekly", "fortnightly", "monthly")


def upgrade() -> None:
    bind = op.get_bind()

    # Create the types first, then reference them with `create_type=False`.
    # Letting `add_column` create them implicitly is how a migration ends up
    # issuing CREATE TYPE twice.
    for name, values in (
        ("property_type", PROPERTY_TYPE),
        ("service_type", SERVICE_TYPE),
        ("recurrence", RECURRENCE),
    ):
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=True)

    property_type = postgresql.ENUM(
        *PROPERTY_TYPE, name="property_type", create_type=False
    )
    service_type = postgresql.ENUM(
        *SERVICE_TYPE, name="service_type", create_type=False
    )

    op.add_column(
        "properties",
        sa.Column(
            "property_type",
            property_type,
            nullable=False,
            server_default="short_term_rental",
        ),
    )
    op.create_index(
        op.f("ix_properties_property_type"), "properties", ["property_type"]
    )

    op.add_column("properties", sa.Column("square_feet", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "square_feet_positive", "properties", "square_feet IS NULL OR square_feet > 0"
    )

    op.add_column(
        "turnovers",
        sa.Column(
            "service_type", service_type, nullable=False, server_default="turnover"
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()

    # Unlike 0004's enum downgrade, nothing has to be refused here: these
    # columns are being dropped whole rather than narrowed, so there is no row
    # left holding a value the old schema cannot express. A residential
    # property reverts to looking like a rental, which is what it looked like
    # before this migration — wrong, but not unrepresentable, and the person
    # running a downgrade is choosing that.
    op.drop_column("turnovers", "service_type")

    op.drop_constraint("square_feet_positive", "properties", type_="check")
    op.drop_column("properties", "square_feet")

    op.drop_index(op.f("ix_properties_property_type"), table_name="properties")
    op.drop_column("properties", "property_type")

    for name in ("recurrence", "service_type", "property_type"):
        postgresql.ENUM(name=name).drop(bind, checkfirst=True)
