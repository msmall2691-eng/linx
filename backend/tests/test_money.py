"""The money tables' shape.

No Stripe integration exists yet (phase 6). What exists now is the schema that
guardrail 2 depends on, and these tests hold it in place: an idempotency key
that cannot be duplicated, an attempt timestamp that can be written before the
network call, and a status that can say "unknown outcome, a human should look".
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    Award,
    PaymentIn,
    PaymentStatus,
    Payout,
    Property,
    Turnover,
    User,
    UserRole,
)


def _turnover_with_award(db: Session) -> tuple[Turnover, Award, User]:
    owner = User(
        email=f"owner-{uuid.uuid4().hex}@example.com",
        hashed_password="x",
        full_name="Owner",
        role=UserRole.OWNER,
    )
    cleaner = User(
        email=f"cleaner-{uuid.uuid4().hex}@example.com",
        hashed_password="x",
        full_name="Cleaner",
        role=UserRole.CLEANER,
    )
    db.add_all([owner, cleaner])
    db.flush()

    prop = Property(
        owner_id=owner.id,
        nickname="Harbor Loft",
        address_line1="5 Harbor Way",
        city="Portland",
        state="ME",
        postal_code="04101",
    )
    db.add(prop)
    db.flush()

    checkout = datetime.now(timezone.utc) + timedelta(days=2)
    turnover = Turnover(property_id=prop.id, checkout_at=checkout)
    db.add(turnover)
    db.flush()

    award = Award(
        turnover_id=turnover.id, cleaner_id=cleaner.id, agreed_price_cents=15_000
    )
    db.add(award)
    db.commit()
    return turnover, award, cleaner


def test_idempotency_key_is_derived_from_a_stable_id_and_cannot_repeat(
    db: Session,
) -> None:
    """Guardrail 2, in schema form.

    A retry must produce the *same* key, which means deriving it from a row id
    that already exists — never a fresh uuid4. The unique index means that even
    a caller who forgets cannot create a second charge row for one award.
    """
    turnover, award, _ = _turnover_with_award(db)
    key = f"charge:award:{award.id}"

    db.add(
        PaymentIn(
            turnover_id=turnover.id,
            amount_cents=15_000,
            platform_fee_cents=2_250,
            idempotency_key=key,
        )
    )
    db.commit()

    other_turnover, _, _ = _turnover_with_award(db)
    db.add(
        PaymentIn(
            turnover_id=other_turnover.id,
            amount_cents=15_000,
            platform_fee_cents=2_250,
            idempotency_key=key,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_a_turnover_can_only_be_charged_once(db: Session) -> None:
    turnover, award, _ = _turnover_with_award(db)
    db.add(
        PaymentIn(
            turnover_id=turnover.id,
            amount_cents=15_000,
            platform_fee_cents=2_250,
            idempotency_key=f"charge:award:{award.id}",
        )
    )
    db.commit()

    db.add(PaymentIn(turnover_id=turnover.id, amount_cents=15_000, platform_fee_cents=2_250))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_an_attempt_can_be_recorded_before_the_call_and_flagged_for_review(
    db: Session,
) -> None:
    """The crash-mid-call shape: attempted, outcome unknown, visible to a human.

    Not assumed successful, not assumed failed, and not silently retried.
    """
    turnover, award, _ = _turnover_with_award(db)
    payment = PaymentIn(
        turnover_id=turnover.id,
        amount_cents=15_000,
        platform_fee_cents=2_250,
        idempotency_key=f"charge:award:{award.id}",
        attempted_at=datetime.now(timezone.utc),
        status=PaymentStatus.REQUIRES_REVIEW,
    )
    db.add(payment)
    db.commit()
    db.refresh(payment)

    assert payment.attempted_at is not None
    assert payment.status is PaymentStatus.REQUIRES_REVIEW
    assert payment.stripe_payment_intent_id is None


def test_the_platform_fee_cannot_exceed_what_was_collected(db: Session) -> None:
    turnover, _, _ = _turnover_with_award(db)
    db.add(
        PaymentIn(turnover_id=turnover.id, amount_cents=10_000, platform_fee_cents=10_001)
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_a_refund_cannot_exceed_what_was_collected(db: Session) -> None:
    turnover, _, _ = _turnover_with_award(db)
    db.add(
        PaymentIn(
            turnover_id=turnover.id,
            amount_cents=10_000,
            platform_fee_cents=1_500,
            refunded_amount_cents=10_001,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_collected_equals_payout_plus_fee(db: Session) -> None:
    """The reconciliation identity, asserted on the schema that has to hold it.

    Phase 6's real reconciliation test runs against Stripe test mode. This one
    just proves the rows can express the identity without a rounding gap —
    integer cents, no floats anywhere.
    """
    turnover, award, cleaner = _turnover_with_award(db)

    amount = award.agreed_price_cents
    fee = amount * 15 // 100

    payment = PaymentIn(
        turnover_id=turnover.id,
        amount_cents=amount,
        platform_fee_cents=fee,
        idempotency_key=f"charge:award:{award.id}",
        status=PaymentStatus.SUCCEEDED,
    )
    db.add(payment)
    db.flush()

    payout = Payout(
        cleaner_id=cleaner.id,
        turnover_id=turnover.id,
        payment_in_id=payment.id,
        amount_cents=amount - fee,
        idempotency_key=f"transfer:award:{award.id}",
        status=PaymentStatus.SUCCEEDED,
    )
    db.add(payout)
    db.commit()

    assert payment.amount_cents == payout.amount_cents + payment.platform_fee_cents
    assert isinstance(payout.amount_cents, int)


def test_a_cleaner_is_paid_once_per_turnover(db: Session) -> None:
    turnover, award, cleaner = _turnover_with_award(db)
    db.add(
        Payout(cleaner_id=cleaner.id, turnover_id=turnover.id, amount_cents=12_750)
    )
    db.commit()

    db.add(
        Payout(cleaner_id=cleaner.id, turnover_id=turnover.id, amount_cents=12_750)
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
