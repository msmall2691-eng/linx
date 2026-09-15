"""task_runs — the scheduled pass records that it ran

Phase 9's one new table, and the smallest of the phase. It exists because
launch readiness has to be *checked* rather than assumed, and one of the things
worth checking is the only part of this product that says nothing when it
stops.

A cron service that was never created, or that has been failing since the last
deploy, is indistinguishable from a quiet week. Three things hang off it and
one is load-bearing rather than a courtesy: without the pass, one-sided reviews
are never revealed, and refusing to answer becomes the way to bury a bad review
(CLAUDE.md).

**One row per task, overwritten.** This is not a log. A log answers "what
happened last April"; this answers "is it running", which is the only question
a launch check asks. `name` is unique because a one-answer question must not
have two rows able to give different answers.

`finished_at`, not started: a pass that begins and dies is not evidence the
work was done, and recording the start would make a task that crashes every
single time look perfectly healthy.

Revision ID: 0008_task_runs
Revises: 0007_disputes
Create Date: 2026-09-15 00:47:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_task_runs"
down_revision: str | None = "0007_disputes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_runs",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_runs")),
        # One row per task, enforced by the database rather than by the one
        # function that happens to write it today.
        sa.UniqueConstraint("name", name=op.f("uq_task_runs_name")),
    )


def downgrade() -> None:
    op.drop_table("task_runs")
