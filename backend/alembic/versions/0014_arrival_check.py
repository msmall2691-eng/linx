"""How far away the cleaner's phone said it was when they tapped arrived.

**Two integers, and deliberately not a position.** `arrival_distance_m` is a
scalar relative to a point the owner already knows — their own property — so it
describes a ring rather than a place, and it cannot be replayed into a trail.
The reading itself is used and thrown away; nothing here stores where anybody
was.

`arrival_accuracy_m` is what makes the distance readable rather than merely
present. A browser that falls back to IP geolocation returns a fix accurate to
tens of kilometres, and a fix like that landing inside the radius is not
evidence of anything — so the accuracy is stored beside the distance, and
`awards.arrival_check` refuses to draw a conclusion from a reading too coarse
to support one.

**It is a claim, not proof.** The coordinate comes from the cleaner's own
browser and can be fabricated by anybody who wants to. That is worth writing
into the schema, because the failure mode of this feature is not a bug: it is
somebody treating a green tick as evidence in a dispute it cannot settle.

Revision ID: 0014_arrival_check
Revises: 0013_job_messages
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_arrival_check"
down_revision = "0013_job_messages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("awards", sa.Column("arrival_distance_m", sa.Integer(), nullable=True))
    op.add_column("awards", sa.Column("arrival_accuracy_m", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("awards", "arrival_accuracy_m")
    op.drop_column("awards", "arrival_distance_m")
