"""Properties — an STR unit belonging to one owner."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.turnover import Turnover
    from app.models.user import User


class Property(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "properties"
    __table_args__ = (
        CheckConstraint("bedrooms >= 0", name="bedrooms_non_negative"),
        CheckConstraint("bathrooms >= 0", name="bathrooms_non_negative"),
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

    bedrooms: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Half-baths are real, so this is not an integer.
    bathrooms: Mapped[float] = mapped_column(Numeric(3, 1), nullable=False, default=1)

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

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Property {self.nickname}>"
