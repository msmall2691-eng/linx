"""Mutual, delayed-reveal reviews.

Neither side's review is visible until both are submitted or a timeout passes —
`visible_at` carries that. Showing a review the moment it lands creates an
incentive to leave a pre-emptive bad one to suppress the other side's, which is
a known failure mode worth designing around from the start rather than patching
later. The reveal itself lives in `app/services/reviews.py`, which is the only
thing that writes `visible_at`; this file is just the shape.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import UserRole

if TYPE_CHECKING:
    from app.models.turnover import Turnover
    from app.models.user import User


class Review(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "reviews"
    __table_args__ = (
        # One review per side per turnover.
        UniqueConstraint("turnover_id", "author_role", name="uq_reviews_turnover_author_role"),
        CheckConstraint("rating >= 1 AND rating <= 5", name="rating_range"),
        CheckConstraint("author_role <> 'admin'", name="author_role_not_admin"),
    )

    turnover_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("turnovers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    author_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: owner or cleaner — which side of the turnover wrote this.
    author_role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )

    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Null until the reveal condition is met. A null here means invisible —
    #: no other flag competes with it.
    visible_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    turnover: Mapped["Turnover"] = relationship()
    author: Mapped["User"] = relationship()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Review {self.author_role.value} {self.rating}/5>"
