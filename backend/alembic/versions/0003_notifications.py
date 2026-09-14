"""notifications — the record that somebody was told, or that nobody was

Phase 5. One table and three enum types.

The `dedupe_key` unique constraint is the load-bearing part: it is what makes
"an event fires once per transition" a property of the database rather than of
whoever wrote the call site. The scheduled reminder job leans on it directly —
it can run every fifteen minutes and the second run through the same window
writes nothing, because the key it derives is the one already there.

The enum types are created and dropped explicitly rather than left to
`create_table`/`drop_table`, following migration 0001. Alembic creates a type
implicitly on the way up but does **not** drop it on the way down, so the
generated version of this migration would downgrade cleanly once and then fail
on the next upgrade with "type already exists" — the kind of breakage that only
appears on the second run, which is the worst kind to discover on a deploy.

Revision ID: 0003_notifications
Revises: 0002_award_lifecycle
Create Date: 2026-09-14 01:45:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '0003_notifications'
down_revision: str | None = '0002_award_lifecycle'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


ENUM_TYPES: dict[str, tuple[str, ...]] = {
    "notification_event": (
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
    ),
    "notification_channel": ("email", "sms"),
    "notification_status": ("pending", "sent", "failed"),
}


def upgrade() -> None:
    bind = op.get_bind()
    for name, values in ENUM_TYPES.items():
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=False)

    op.create_table(
        'notifications',
        sa.Column(
            'event',
            postgresql.ENUM(
                *ENUM_TYPES["notification_event"],
                name='notification_event',
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column('recipient_id', sa.UUID(), nullable=False),
        sa.Column('destination', sa.String(length=320), nullable=False),
        sa.Column(
            'channel',
            postgresql.ENUM(
                *ENUM_TYPES["notification_channel"],
                name='notification_channel',
                create_type=False,
            ),
            server_default='email',
            nullable=False,
        ),
        sa.Column('turnover_id', sa.UUID(), nullable=True),
        sa.Column('dedupe_key', sa.String(length=255), nullable=False),
        sa.Column('subject', sa.String(length=255), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column(
            'status',
            postgresql.ENUM(
                *ENUM_TYPES["notification_status"],
                name='notification_status',
                create_type=False,
            ),
            server_default='pending',
            nullable=False,
        ),
        sa.Column('attempted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('failure_message', sa.Text(), nullable=True),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['recipient_id'],
            ['users.id'],
            name=op.f('fk_notifications_recipient_id_users'),
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['turnover_id'],
            ['turnovers.id'],
            name=op.f('fk_notifications_turnover_id_turnovers'),
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_notifications')),
        sa.UniqueConstraint('dedupe_key', name=op.f('uq_notifications_dedupe_key')),
    )
    op.create_index(op.f('ix_notifications_event'), 'notifications', ['event'], unique=False)
    op.create_index(
        op.f('ix_notifications_recipient_id'), 'notifications', ['recipient_id'], unique=False
    )
    op.create_index(op.f('ix_notifications_status'), 'notifications', ['status'], unique=False)
    op.create_index(
        'ix_notifications_status_created_at',
        'notifications',
        ['status', 'created_at'],
        unique=False,
    )
    op.create_index(
        op.f('ix_notifications_turnover_id'), 'notifications', ['turnover_id'], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_notifications_turnover_id'), table_name='notifications')
    op.drop_index('ix_notifications_status_created_at', table_name='notifications')
    op.drop_index(op.f('ix_notifications_status'), table_name='notifications')
    op.drop_index(op.f('ix_notifications_recipient_id'), table_name='notifications')
    op.drop_index(op.f('ix_notifications_event'), table_name='notifications')
    op.drop_table('notifications')

    bind = op.get_bind()
    for name in ENUM_TYPES:
        postgresql.ENUM(name=name).drop(bind, checkfirst=False)
