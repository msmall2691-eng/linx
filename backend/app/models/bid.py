"""Bids — a cleaner names a price on an open turnover.

One bid per cleaner per turnover, enforced by a unique constraint; changing your
number updates the existing row rather than stacking a second one, so an owner
never sees the same cleaner twice on one job.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Enum, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import BidStatus

if TYPE_CHECKING:
    from app.models.turnover import Turnover
    from app.models.user import User


class Bid(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "bids"
    __table_args__ = (
        UniqueConstraint("turnover_id", "cleaner_id", name="uq_bids_turnover_cleaner"),
        CheckConstraint("price_cents > 0", name="price_positive"),
    )

    turnover_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("turnovers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    cleaner_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    #: Integer cents. Never a float.
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[BidStatus] = mapped_column(
        Enum(BidStatus, name="bid_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=BidStatus.SUBMITTED,
        server_default=BidStatus.SUBMITTED.value,
        index=True,
    )

    turnover: Mapped["Turnover"] = relationship(back_populates="bids")
    cleaner: Mapped["User"] = relationship()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Bid {self.id} {self.price_cents}c {self.status.value}>"
