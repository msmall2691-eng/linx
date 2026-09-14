"""Properties — an STR unit belonging to one owner."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from datetime import time

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    Time,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import PropertyType

if TYPE_CHECKING:
    from app.models.calendar import PropertyCalendar
    from app.models.turnover import Turnover
    from app.models.user import User


class Property(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "properties"
    __table_args__ = (
        CheckConstraint("bedrooms >= 0", name="bedrooms_non_negative"),
        CheckConstraint("bathrooms >= 0", name="bathrooms_non_negative"),
        CheckConstraint(
            "square_feet IS NULL OR square_feet > 0", name="square_feet_positive"
        ),
    )

    owner_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    nickname: Mapped[str] = mapped_column(String(120), nullable=False)

    address_line1: Mapped[str] = mapped_column(String(200), nullable=False)
    address_line2: Mapped[str | None] = mapped_column(String(200), nullable=True)
    city: Mapped[str] = mapped_column(String(120), nullable=False)
    state: Mapped[str] = mapped_column(String(2), nullable=False)
    postal_code: Mapped[str] = mapped_column(String(12), nullable=False)

    lat: Mapped[float | None] = mapped_column(Numeric(9, 6), nullable=True)
    lng: Mapped[float | None] = mapped_column(Numeric(9, 6), nullable=True)

    #: A short-term rental or a home. Decides how its jobs are scheduled —
    #: a rental's clean is defined by the gap between guests, a home's by the
    #: date somebody picked — and therefore which fields the posting form even
    #: asks about.
    property_type: Mapped[PropertyType] = mapped_column(
        Enum(
            PropertyType,
            name="property_type",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=PropertyType.SHORT_TERM_RENTAL,
        server_default=PropertyType.SHORT_TERM_RENTAL.value,
        index=True,
    )

    bedrooms: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Half-baths are real, so this is not an integer.
    bathrooms: Mapped[float] = mapped_column(Numeric(3, 1), nullable=False, default=1)
    #: Optional, because plenty of owners genuinely do not know it — and a
    #: required field somebody has to guess at produces a number worse than no
    #: number. Cleaners price on it when it is there, so it is asked for.
    square_feet: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: **What an all-day calendar cannot tell us.** Airbnb and VRBO export
    #: bookings as whole days — a guest leaves "on the 7th" with no hour
    #: attached — but the urgency ladder is measured in hours, so a synced
    #: turnover needs a time from somewhere. These are that somewhere: the
    #: house's own checkout and checkin policy, which the owner knows and the
    #: feed does not. Region-local, stored as a plain time, with the defaults
    #: most listings use.
    default_checkout_time: Mapped[time] = mapped_column(
        Time, nullable=False, default=time(11, 0), server_default="11:00:00"
    )
    default_checkin_time: Mapped[time] = mapped_column(
        Time, nullable=False, default=time(16, 0), server_default="16:00:00"
    )

    #: Gate codes, lockbox locations, parking notes. Sensitive: only the owner,
    #: an admin, and the awarded cleaner should ever see this field. The access
    #: rule lands with the property endpoints in phase 2.
    access_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    cleaning_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    owner: Mapped["User"] = relationship(back_populates="properties")
    turnovers: Mapped[list["Turnover"]] = relationship(
        back_populates="property",
        cascade="all, delete-orphan",
    )
    calendars: Mapped[list["PropertyCalendar"]] = relationship(
        back_populates="property",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Property {self.nickname}>"
