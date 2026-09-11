"""Turnovers — one cleaning job between a checkout and the next checkin.

`urgency` is derived from how close `checkout_at` is to `checkin_at` (or to now,
for a standing vacancy with no next guest booked). The closer, the more urgent.
That ladder is the product's core pricing and priority signal — the derivation
itself lands in phase 2; the column shape is settled here.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import TurnoverStatus, TurnoverUrgency

if TYPE_CHECKING:
    from app.models.award import Award
    from app.models.bid import Bid
    from app.models.property import Property


class Turnover(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "turnovers"
    __table_args__ = (
        CheckConstraint(
            "checkin_at IS NULL OR checkin_at >= checkout_at",
            name="checkin_after_checkout",
        ),
        CheckConstraint(
            "owner_budget_cents IS NULL OR owner_budget_cents >= 0",
            name="budget_non_negative",
        ),
        # The bench board reads open turnovers ordered by urgency then checkout.
        Index("ix_turnovers_status_checkout_at", "status", "checkout_at"),
    )

    property_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("properties.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    checkout_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Null means a standing vacancy — no next guest booked yet.
    checkin_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: True when the next guest arrives the same day the last one leaves. The
    #: hardest turnovers to staff and the ones that price highest.
    is_same_day: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    status: Mapped[TurnoverStatus] = mapped_column(
        Enum(
            TurnoverStatus,
            name="turnover_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=TurnoverStatus.DRAFT,
        server_default=TurnoverStatus.DRAFT.value,
        index=True,
    )

    urgency: Mapped[TurnoverUrgency] = mapped_column(
        Enum(
            TurnoverUrgency,
            name="turnover_urgency",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=TurnoverUrgency.STANDARD,
        server_default=TurnoverUrgency.STANDARD.value,
        index=True,
    )

    #: Integer cents. Optional guide price the owner posts with the job.
    owner_budget_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Set when a cleaner backs out after being awarded. A late cancellation
    #: re-opens the turnover urgently and alerts the owner and admin — never
    #: silently (phase 4).
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    property: Mapped["Property"] = relationship(back_populates="turnovers")
    bids: Mapped[list["Bid"]] = relationship(
        back_populates="turnover",
        cascade="all, delete-orphan",
    )
    award: Mapped["Award | None"] = relationship(
        back_populates="turnover",
        uselist=False,
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Turnover {self.id} {self.status.value}/{self.urgency.value}>"
