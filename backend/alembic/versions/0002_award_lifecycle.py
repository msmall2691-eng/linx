"""award lifecycle — a booking that can come undone, and a re-post that shows it

Phase 4. Three changes, all of them because an award is no longer assumed to be
the end of the story:

* `awards` gains `cancelled_at`, `cancellation_reason`, `cancelled_by_id` and
  `was_no_show`. A cancelled award is kept, not deleted — who was booked and who
  backed out is the history a dispute is argued from.
* the unique index on `awards.turnover_id` becomes **partial**
  (`WHERE cancelled_at IS NULL`), so a re-opened turnover can be awarded again
  while "at most one live award per turnover" — guardrail 1's backstop — still
  holds. The plain index on the column stays for the lookups.
* `turnovers` gains `reopened_at`, the input that lets the urgency ladder treat
  a re-staffed job like a standing vacancy: once nobody is booked again, the
  time left until checkout counts.

Revision ID: 0002_award_lifecycle
Revises: 0001_initial_schema
Create Date: 2026-09-13 20:05:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0002_award_lifecycle'
down_revision: str | None = '0001_initial_schema'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('awards', sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('awards', sa.Column('cancellation_reason', sa.Text(), nullable=True))
    op.add_column('awards', sa.Column('cancelled_by_id', sa.UUID(), nullable=True))
    op.add_column(
        'awards',
        sa.Column(
            'was_no_show',
            sa.Boolean(),
            server_default='false',
            nullable=False,
        ),
    )
    op.create_foreign_key(
        op.f('fk_awards_cancelled_by_id_users'),
        'awards',
        'users',
        ['cancelled_by_id'],
        ['id'],
        ondelete='SET NULL',
    )

    # The old index was UNIQUE on turnover_id outright. Replace it with a plain
    # index for lookups plus a partial unique index for the invariant.
    op.drop_index(op.f('ix_awards_turnover_id'), table_name='awards')
    op.create_index(op.f('ix_awards_turnover_id'), 'awards', ['turnover_id'], unique=False)
    op.create_index(
        'uq_awards_live_turnover',
        'awards',
        ['turnover_id'],
        unique=True,
        postgresql_where=sa.text('cancelled_at IS NULL'),
    )

    op.add_column(
        'turnovers', sa.Column('reopened_at', sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column('turnovers', 'reopened_at')

    op.drop_index(
        'uq_awards_live_turnover',
        table_name='awards',
        postgresql_where=sa.text('cancelled_at IS NULL'),
    )
    op.drop_index(op.f('ix_awards_turnover_id'), table_name='awards')
    # Going back means the old invariant again: one award per turnover, ever.
    # Cancelled awards for a turnover that has since been re-awarded would make
    # that index impossible to build, which is the honest outcome — the data no
    # longer fits the shape being restored.
    op.create_index(op.f('ix_awards_turnover_id'), 'awards', ['turnover_id'], unique=True)

    op.drop_constraint(op.f('fk_awards_cancelled_by_id_users'), 'awards', type_='foreignkey')
    op.drop_column('awards', 'was_no_show')
    op.drop_column('awards', 'cancelled_by_id')
    op.drop_column('awards', 'cancellation_reason')
    op.drop_column('awards', 'cancelled_at')
