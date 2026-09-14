"""The two money legs.

`PaymentIn` collects from the owner; `Payout` records what reached the cleaner.

These are **not** two independent transactions reconciled later. Phase 6 charges
a single Stripe Connect **destination charge**: one `PaymentIntent` with
`transfer_data` pointing at the cleaner's connected account and
`application_fee_amount` as the platform cut, settled atomically by Stripe. The
`Payout` row is the record of the transfer Stripe made as part of that charge,
which is why it carries the payment it came from — collected always equals
payout plus platform fee, with nowhere for drift to hide.

Both tables carry guardrail 2's two fields:

* `idempotency_key` — derived from a stable database id already on hand (the
  `Award` id), **never** freshly generated, so a retry produces the same key and
  Stripe recognizes it as a retry instead of a second charge. Unique, so a
  second row with the same key cannot be created either.
* `attempted_at` — written and committed *before* the network call. A crash
  mid-call leaves the row visibly flagged (status `requires_review`) rather than
  looking untouched. An unknown outcome is never assumed successful and never
  assumed failed.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import PaymentStatus

if TYPE_CHECKING:
    from app.models.turnover import Turnover
    from app.models.user import User


class PaymentIn(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Money collected from the property owner."""

    __tablename__ = "payments_in"
    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="amount_positive"),
        CheckConstraint("platform_fee_cents >= 0", name="platform_fee_non_negative"),
        CheckConstraint("platform_fee_cents <= amount_cents", name="platform_fee_within_amount"),
        CheckConstraint(
            "refunded_amount_cents >= 0 AND refunded_amount_cents <= amount_cents",
            name="refund_within_amount",
        ),
    )

    turnover_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("turnovers.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
        index=True,
    )

    stripe_payment_intent_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, unique=True
    )
    #: Hosted Checkout: the owner enters card details on Stripe's page, never
    #: on ours. Same reasoning as Checkr holding the SSN and Express holding the
    #: 1099 — card data we never receive is card data we cannot lose.
    stripe_checkout_session_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, unique=True
    )

    #: Integer cents throughout.
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    platform_fee_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    refunded_amount_cents: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    status: Mapped[PaymentStatus] = mapped_column(
        Enum(
            PaymentStatus,
            name="payment_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=PaymentStatus.PENDING,
        server_default=PaymentStatus.PENDING.value,
        index=True,
    )

    #: Guardrail 2. Derived from the Award id; unique so a duplicate row is
    #: impossible even if the derivation is called twice.
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    #: Guardrail 2. Written and committed before the Stripe call.
    attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    turnover: Mapped["Turnover"] = relationship()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<PaymentIn {self.amount_cents}c {self.status.value}>"


class Payout(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Money that reached the cleaner's connected account."""

    __tablename__ = "payouts"
    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="amount_positive"),
        CheckConstraint(
            "reversed_amount_cents >= 0 AND reversed_amount_cents <= amount_cents",
            name="reversal_within_amount",
        ),
    )

    cleaner_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    turnover_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("turnovers.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
        index=True,
    )
    #: The collection this transfer was part of. Present because the two legs
    #: are one Stripe transaction, not two ledgers to reconcile.
    payment_in_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("payments_in.id", ondelete="SET NULL"),
        nullable=True,
    )

    #: Created by Stripe as part of the destination charge, not by a second
    #: call from here. Read back off the charge, which is why a payout cannot
    #: exist without the payment it came from.
    stripe_transfer_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, unique=True
    )
    #: Set when a refund reverses this transfer. The row is kept — what was
    #: paid and then clawed back is the history a dispute is argued from, the
    #: same reason a cancelled award is cancelled rather than deleted.
    reversed_amount_cents: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)

    status: Mapped[PaymentStatus] = mapped_column(
        Enum(
            PaymentStatus,
            name="payment_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=PaymentStatus.PENDING,
        server_default=PaymentStatus.PENDING.value,
        index=True,
    )

    #: Guardrail 2, same contract as PaymentIn.
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    cleaner: Mapped["User"] = relationship()
    turnover: Mapped["Turnover"] = relationship()
    payment_in: Mapped["PaymentIn | None"] = relationship()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Payout {self.amount_cents}c {self.status.value}>"
