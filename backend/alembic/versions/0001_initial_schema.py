"""initial schema — every table for v1, shape only

Phase 1 creates the full schema from CLAUDE.md: users and role auth, plus
the tables later phases fill in. Tables for unbuilt phases are empty on
purpose — the shape is settled now, the behavior arrives with its phase.

Revision ID: 0001_initial_schema
Revises: None
Create Date: 2026-09-11 18:35:37.867004

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '0001_initial_schema'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Postgres enum types, created once here and referenced by every column that
# uses them. `create_type=False` on the column definitions below keeps
# op.create_table from trying to create them a second time.
ENUM_TYPES: dict[str, tuple[str, ...]] = {
    "user_role": ("owner", "cleaner", "admin"),
    "verification_status": ("not_started", "pending", "approved", "rejected"),
    "turnover_status": ("draft", "open", "awarded", "in_progress", "completed", "cancelled"),
    "turnover_urgency": ("standard", "soon", "urgent", "same_day"),
    "bid_status": ("submitted", "withdrawn", "accepted", "declined"),
    "document_type": ("id", "insurance", "reference"),
    "document_status": ("pending", "approved", "rejected"),
    "payment_status": (
        "pending",
        "processing",
        "succeeded",
        "failed",
        "refunded",
        "requires_review",
    ),
}


def upgrade() -> None:
    bind = op.get_bind()
    for name, values in ENUM_TYPES.items():
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=True)

    op.create_table('users',
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('hashed_password', sa.String(length=255), nullable=False),
    sa.Column('full_name', sa.String(length=200), nullable=False),
    sa.Column('phone', sa.String(length=32), nullable=True),
    sa.Column('role', postgresql.ENUM('owner', 'cleaner', 'admin', name='user_role', create_type=False), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users'))
    )
    op.create_index(op.f('ix_users_email'), 'users', ['email'], unique=True)
    op.create_index(op.f('ix_users_role'), 'users', ['role'], unique=False)
    op.create_table('cleaner_profiles',
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('bio', sa.Text(), nullable=True),
    sa.Column('service_lat', sa.Numeric(precision=9, scale=6), nullable=True),
    sa.Column('service_lng', sa.Numeric(precision=9, scale=6), nullable=True),
    sa.Column('service_radius_miles', sa.Integer(), nullable=False),
    sa.Column('id_verification_status', postgresql.ENUM('not_started', 'pending', 'approved', 'rejected', name='verification_status', create_type=False), server_default='not_started', nullable=False),
    sa.Column('background_check_status', postgresql.ENUM('not_started', 'pending', 'approved', 'rejected', name='verification_status', create_type=False), server_default='not_started', nullable=False),
    sa.Column('background_check_provider_ref', sa.String(length=128), nullable=True),
    sa.Column('has_insurance_on_file', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('can_take_jobs', sa.Boolean(), sa.Computed("id_verification_status = 'approved'::verification_status AND background_check_status = 'approved'::verification_status", persisted=True), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('service_lat >= -90 AND service_lat <= 90', name=op.f('ck_cleaner_profiles_service_lat_range')),
    sa.CheckConstraint('service_lng >= -180 AND service_lng <= 180', name=op.f('ck_cleaner_profiles_service_lng_range')),
    sa.CheckConstraint('service_radius_miles > 0 AND service_radius_miles <= 200', name=op.f('ck_cleaner_profiles_service_radius_sane')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_cleaner_profiles_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_cleaner_profiles'))
    )
    op.create_index(op.f('ix_cleaner_profiles_user_id'), 'cleaner_profiles', ['user_id'], unique=True)
    op.create_table('properties',
    sa.Column('owner_id', sa.UUID(), nullable=False),
    sa.Column('nickname', sa.String(length=120), nullable=False),
    sa.Column('address_line1', sa.String(length=200), nullable=False),
    sa.Column('address_line2', sa.String(length=200), nullable=True),
    sa.Column('city', sa.String(length=120), nullable=False),
    sa.Column('state', sa.String(length=2), nullable=False),
    sa.Column('postal_code', sa.String(length=12), nullable=False),
    sa.Column('lat', sa.Numeric(precision=9, scale=6), nullable=True),
    sa.Column('lng', sa.Numeric(precision=9, scale=6), nullable=True),
    sa.Column('bedrooms', sa.Integer(), nullable=False),
    sa.Column('bathrooms', sa.Numeric(precision=3, scale=1), nullable=False),
    sa.Column('access_notes', sa.Text(), nullable=True),
    sa.Column('cleaning_notes', sa.Text(), nullable=True),
    sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('bathrooms >= 0', name=op.f('ck_properties_bathrooms_non_negative')),
    sa.CheckConstraint('bedrooms >= 0', name=op.f('ck_properties_bedrooms_non_negative')),
    sa.ForeignKeyConstraint(['owner_id'], ['users.id'], name=op.f('fk_properties_owner_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_properties'))
    )
    op.create_index(op.f('ix_properties_owner_id'), 'properties', ['owner_id'], unique=False)
    op.create_table('documents',
    sa.Column('cleaner_profile_id', sa.UUID(), nullable=False),
    sa.Column('type', postgresql.ENUM('id', 'insurance', 'reference', name='document_type', create_type=False), nullable=False),
    sa.Column('status', postgresql.ENUM('pending', 'approved', 'rejected', name='document_status', create_type=False), server_default='pending', nullable=False),
    sa.Column('storage_key', sa.String(length=512), nullable=False),
    sa.Column('original_filename', sa.String(length=255), nullable=True),
    sa.Column('content_type', sa.String(length=128), nullable=True),
    sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('reviewed_by_id', sa.UUID(), nullable=True),
    sa.Column('review_notes', sa.Text(), nullable=True),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['cleaner_profile_id'], ['cleaner_profiles.id'], name=op.f('fk_documents_cleaner_profile_id_cleaner_profiles'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['reviewed_by_id'], ['users.id'], name=op.f('fk_documents_reviewed_by_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_documents'))
    )
    op.create_index(op.f('ix_documents_cleaner_profile_id'), 'documents', ['cleaner_profile_id'], unique=False)
    op.create_index(op.f('ix_documents_status'), 'documents', ['status'], unique=False)
    op.create_table('turnovers',
    sa.Column('property_id', sa.UUID(), nullable=False),
    sa.Column('checkout_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('checkin_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('is_same_day', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('status', postgresql.ENUM('draft', 'open', 'awarded', 'in_progress', 'completed', 'cancelled', name='turnover_status', create_type=False), server_default='draft', nullable=False),
    sa.Column('urgency', postgresql.ENUM('standard', 'soon', 'urgent', 'same_day', name='turnover_urgency', create_type=False), server_default='standard', nullable=False),
    sa.Column('owner_budget_cents', sa.Integer(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancellation_reason', sa.Text(), nullable=True),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('checkin_at IS NULL OR checkin_at >= checkout_at', name=op.f('ck_turnovers_checkin_after_checkout')),
    sa.CheckConstraint('owner_budget_cents IS NULL OR owner_budget_cents >= 0', name=op.f('ck_turnovers_budget_non_negative')),
    sa.ForeignKeyConstraint(['property_id'], ['properties.id'], name=op.f('fk_turnovers_property_id_properties'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_turnovers'))
    )
    op.create_index(op.f('ix_turnovers_property_id'), 'turnovers', ['property_id'], unique=False)
    op.create_index(op.f('ix_turnovers_status'), 'turnovers', ['status'], unique=False)
    op.create_index('ix_turnovers_status_checkout_at', 'turnovers', ['status', 'checkout_at'], unique=False)
    op.create_index(op.f('ix_turnovers_urgency'), 'turnovers', ['urgency'], unique=False)
    op.create_table('bids',
    sa.Column('turnover_id', sa.UUID(), nullable=False),
    sa.Column('cleaner_id', sa.UUID(), nullable=False),
    sa.Column('price_cents', sa.Integer(), nullable=False),
    sa.Column('message', sa.Text(), nullable=True),
    sa.Column('status', postgresql.ENUM('submitted', 'withdrawn', 'accepted', 'declined', name='bid_status', create_type=False), server_default='submitted', nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('price_cents > 0', name=op.f('ck_bids_price_positive')),
    sa.ForeignKeyConstraint(['cleaner_id'], ['users.id'], name=op.f('fk_bids_cleaner_id_users'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['turnover_id'], ['turnovers.id'], name=op.f('fk_bids_turnover_id_turnovers'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_bids')),
    sa.UniqueConstraint('turnover_id', 'cleaner_id', name='uq_bids_turnover_cleaner')
    )
    op.create_index(op.f('ix_bids_cleaner_id'), 'bids', ['cleaner_id'], unique=False)
    op.create_index(op.f('ix_bids_status'), 'bids', ['status'], unique=False)
    op.create_index(op.f('ix_bids_turnover_id'), 'bids', ['turnover_id'], unique=False)
    op.create_table('payments_in',
    sa.Column('turnover_id', sa.UUID(), nullable=False),
    sa.Column('stripe_payment_intent_id', sa.String(length=255), nullable=True),
    sa.Column('amount_cents', sa.Integer(), nullable=False),
    sa.Column('platform_fee_cents', sa.Integer(), nullable=False),
    sa.Column('refunded_amount_cents', sa.Integer(), server_default='0', nullable=False),
    sa.Column('status', postgresql.ENUM('pending', 'processing', 'succeeded', 'failed', 'refunded', 'requires_review', name='payment_status', create_type=False), server_default='pending', nullable=False),
    sa.Column('idempotency_key', sa.String(length=255), nullable=True),
    sa.Column('attempted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('failure_message', sa.Text(), nullable=True),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('amount_cents > 0', name=op.f('ck_payments_in_amount_positive')),
    sa.CheckConstraint('platform_fee_cents <= amount_cents', name=op.f('ck_payments_in_platform_fee_within_amount')),
    sa.CheckConstraint('platform_fee_cents >= 0', name=op.f('ck_payments_in_platform_fee_non_negative')),
    sa.CheckConstraint('refunded_amount_cents >= 0 AND refunded_amount_cents <= amount_cents', name=op.f('ck_payments_in_refund_within_amount')),
    sa.ForeignKeyConstraint(['turnover_id'], ['turnovers.id'], name=op.f('fk_payments_in_turnover_id_turnovers'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_payments_in')),
    sa.UniqueConstraint('idempotency_key', name=op.f('uq_payments_in_idempotency_key')),
    sa.UniqueConstraint('stripe_payment_intent_id', name=op.f('uq_payments_in_stripe_payment_intent_id'))
    )
    op.create_index(op.f('ix_payments_in_status'), 'payments_in', ['status'], unique=False)
    op.create_index(op.f('ix_payments_in_turnover_id'), 'payments_in', ['turnover_id'], unique=True)
    op.create_table('reviews',
    sa.Column('turnover_id', sa.UUID(), nullable=False),
    sa.Column('author_id', sa.UUID(), nullable=False),
    sa.Column('author_role', postgresql.ENUM('owner', 'cleaner', 'admin', name='user_role', create_type=False), nullable=False),
    sa.Column('rating', sa.Integer(), nullable=False),
    sa.Column('text', sa.Text(), nullable=True),
    sa.Column('visible_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("author_role <> 'admin'", name=op.f('ck_reviews_author_role_not_admin')),
    sa.CheckConstraint('rating >= 1 AND rating <= 5', name=op.f('ck_reviews_rating_range')),
    sa.ForeignKeyConstraint(['author_id'], ['users.id'], name=op.f('fk_reviews_author_id_users'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['turnover_id'], ['turnovers.id'], name=op.f('fk_reviews_turnover_id_turnovers'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_reviews')),
    sa.UniqueConstraint('turnover_id', 'author_role', name='uq_reviews_turnover_author_role')
    )
    op.create_index(op.f('ix_reviews_author_id'), 'reviews', ['author_id'], unique=False)
    op.create_index(op.f('ix_reviews_turnover_id'), 'reviews', ['turnover_id'], unique=False)
    op.create_index(op.f('ix_reviews_visible_at'), 'reviews', ['visible_at'], unique=False)
    op.create_table('awards',
    sa.Column('turnover_id', sa.UUID(), nullable=False),
    sa.Column('cleaner_id', sa.UUID(), nullable=False),
    sa.Column('bid_id', sa.UUID(), nullable=True),
    sa.Column('agreed_price_cents', sa.Integer(), nullable=False),
    sa.Column('awarded_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('agreed_price_cents > 0', name=op.f('ck_awards_agreed_price_positive')),
    sa.ForeignKeyConstraint(['bid_id'], ['bids.id'], name=op.f('fk_awards_bid_id_bids'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['cleaner_id'], ['users.id'], name=op.f('fk_awards_cleaner_id_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['turnover_id'], ['turnovers.id'], name=op.f('fk_awards_turnover_id_turnovers'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_awards'))
    )
    op.create_index(op.f('ix_awards_cleaner_id'), 'awards', ['cleaner_id'], unique=False)
    op.create_index(op.f('ix_awards_turnover_id'), 'awards', ['turnover_id'], unique=True)
    op.create_table('payouts',
    sa.Column('cleaner_id', sa.UUID(), nullable=False),
    sa.Column('turnover_id', sa.UUID(), nullable=False),
    sa.Column('payment_in_id', sa.UUID(), nullable=True),
    sa.Column('stripe_transfer_id', sa.String(length=255), nullable=True),
    sa.Column('amount_cents', sa.Integer(), nullable=False),
    sa.Column('status', postgresql.ENUM('pending', 'processing', 'succeeded', 'failed', 'refunded', 'requires_review', name='payment_status', create_type=False), server_default='pending', nullable=False),
    sa.Column('idempotency_key', sa.String(length=255), nullable=True),
    sa.Column('attempted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('failure_message', sa.Text(), nullable=True),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('amount_cents > 0', name=op.f('ck_payouts_amount_positive')),
    sa.ForeignKeyConstraint(['cleaner_id'], ['users.id'], name=op.f('fk_payouts_cleaner_id_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['payment_in_id'], ['payments_in.id'], name=op.f('fk_payouts_payment_in_id_payments_in'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['turnover_id'], ['turnovers.id'], name=op.f('fk_payouts_turnover_id_turnovers'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_payouts')),
    sa.UniqueConstraint('idempotency_key', name=op.f('uq_payouts_idempotency_key')),
    sa.UniqueConstraint('stripe_transfer_id', name=op.f('uq_payouts_stripe_transfer_id'))
    )
    op.create_index(op.f('ix_payouts_cleaner_id'), 'payouts', ['cleaner_id'], unique=False)
    op.create_index(op.f('ix_payouts_status'), 'payouts', ['status'], unique=False)
    op.create_index(op.f('ix_payouts_turnover_id'), 'payouts', ['turnover_id'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_payouts_turnover_id'), table_name='payouts')
    op.drop_index(op.f('ix_payouts_status'), table_name='payouts')
    op.drop_index(op.f('ix_payouts_cleaner_id'), table_name='payouts')
    op.drop_table('payouts')
    op.drop_index(op.f('ix_awards_turnover_id'), table_name='awards')
    op.drop_index(op.f('ix_awards_cleaner_id'), table_name='awards')
    op.drop_table('awards')
    op.drop_index(op.f('ix_reviews_visible_at'), table_name='reviews')
    op.drop_index(op.f('ix_reviews_turnover_id'), table_name='reviews')
    op.drop_index(op.f('ix_reviews_author_id'), table_name='reviews')
    op.drop_table('reviews')
    op.drop_index(op.f('ix_payments_in_turnover_id'), table_name='payments_in')
    op.drop_index(op.f('ix_payments_in_status'), table_name='payments_in')
    op.drop_table('payments_in')
    op.drop_index(op.f('ix_bids_turnover_id'), table_name='bids')
    op.drop_index(op.f('ix_bids_status'), table_name='bids')
    op.drop_index(op.f('ix_bids_cleaner_id'), table_name='bids')
    op.drop_table('bids')
    op.drop_index(op.f('ix_turnovers_urgency'), table_name='turnovers')
    op.drop_index('ix_turnovers_status_checkout_at', table_name='turnovers')
    op.drop_index(op.f('ix_turnovers_status'), table_name='turnovers')
    op.drop_index(op.f('ix_turnovers_property_id'), table_name='turnovers')
    op.drop_table('turnovers')
    op.drop_index(op.f('ix_documents_status'), table_name='documents')
    op.drop_index(op.f('ix_documents_cleaner_profile_id'), table_name='documents')
    op.drop_table('documents')
    op.drop_index(op.f('ix_properties_owner_id'), table_name='properties')
    op.drop_table('properties')
    op.drop_index(op.f('ix_cleaner_profiles_user_id'), table_name='cleaner_profiles')
    op.drop_table('cleaner_profiles')
    op.drop_index(op.f('ix_users_role'), table_name='users')
    op.drop_index(op.f('ix_users_email'), table_name='users')
    op.drop_table('users')

    bind = op.get_bind()
    for name in ENUM_TYPES:
        postgresql.ENUM(name=name).drop(bind, checkfirst=True)
