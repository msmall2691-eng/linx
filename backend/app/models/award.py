"""Awards — one turnover goes to exactly one cleaner, once.

**Guardrail 1 governs writes to this table.** The `turnover_id` unique
constraint below is the database's backstop, not the mechanism: the mechanism is
`SELECT ... FOR UPDATE` on the `Turnover` row taken *before* checking whether it
is already awarded, with the check and the insert inside that same lock and
transaction (see CLAUDE.md). The constraint turns a missed lock into a loud
error instead of a second award, which is the right failure — but a route that
relies on catching the constraint violation has already lost the race it was
supposed to prevent.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.bid import Bid
    from app.models.turnover import Turnover
    from app.models.user import User


class Award(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "awards"
    __table_args__ = (
        CheckConstraint("agreed_price_cents > 0", name="agreed_price_positive"),
    )

    #: Unique — one award per turnover, forever.
    turnover_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("turnovers.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    cleaner_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    #: The bid that won, kept for the audit trail.
    bid_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("bids.id", ondelete="SET NULL"),
        nullable=True,
    )

    #: Integer cents, frozen at accept time — the bid may change later, this
    #: does not. This id is also the stable seed for the Stripe idempotency key
    #: in phase 6 (guardrail 2).
    agreed_price_cents: Mapped[int] = mapped_column(Integer, nullable=False)

    awarded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    turnover: Mapped["Turnover"] = relationship(back_populates="award")
    cleaner: Mapped["User"] = relationship()
    bid: Mapped["Bid | None"] = relationship()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Award turnover={self.turnover_id} cleaner={self.cleaner_id}>"
