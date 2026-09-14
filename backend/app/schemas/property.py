"""Request and response shapes for properties.

`access_notes` holds gate codes, lockbox locations, and alarm instructions.
`PropertyOut` is an **owner-facing** shape and includes it. Any cleaner-facing
serializer added later must leave it out until that cleaner has actually been
awarded the turnover — a cleaner browsing the bench board has no business
holding the code to a house they have not been hired to clean.
"""

from __future__ import annotations

import uuid
from datetime import datetime, time
from decimal import Decimal
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.enums import PropertyType


class PropertyBase(BaseModel):
    nickname: str = Field(min_length=1, max_length=120)
    address_line1: str = Field(min_length=1, max_length=200)
    address_line2: str | None = Field(default=None, max_length=200)
    city: str = Field(min_length=1, max_length=120)
    state: str = Field(min_length=2, max_length=2)
    postal_code: str = Field(min_length=3, max_length=12)

    lat: Decimal | None = Field(default=None, ge=-90, le=90)
    lng: Decimal | None = Field(default=None, ge=-180, le=180)

    #: A short-term rental or a home. Defaults to a rental, which is what the
    #: product was built for and what every property created before this
    #: existed actually is.
    property_type: PropertyType = PropertyType.SHORT_TERM_RENTAL

    bedrooms: int = Field(default=1, ge=0, le=50)
    bathrooms: Decimal = Field(default=Decimal("1.0"), ge=0, le=50)
    #: Optional, because plenty of owners do not know it — and a required field
    #: somebody has to guess at produces a number worse than no number.
    square_feet: int | None = Field(default=None, gt=0, le=100_000)

    #: **What an all-day calendar cannot tell us.** Airbnb and VRBO export
    #: whole days — a guest leaves "on the 7th" with no hour — but the urgency
    #: ladder is measured in hours. These are the house's own policy, which the
    #: owner knows and the feed does not, and they are region-local.
    default_checkout_time: time = time(11, 0)
    default_checkin_time: time = time(16, 0)

    access_notes: str | None = None
    cleaning_notes: str | None = None

    @field_validator("state")
    @classmethod
    def uppercase_state(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("bathrooms")
    @classmethod
    def half_baths_only(cls, v: Decimal) -> Decimal:
        """Half-baths are real; quarter-baths are a typo."""
        if (v * 2) % 1 != 0:
            raise ValueError("bathrooms must be a whole or half number")
        return v


class PropertyCreate(PropertyBase):
    pass


class PropertyUpdate(BaseModel):
    """Every field optional — a PATCH touches only what it names."""

    nickname: str | None = Field(default=None, min_length=1, max_length=120)
    address_line1: str | None = Field(default=None, min_length=1, max_length=200)
    address_line2: str | None = Field(default=None, max_length=200)
    city: str | None = Field(default=None, min_length=1, max_length=120)
    state: str | None = Field(default=None, min_length=2, max_length=2)
    postal_code: str | None = Field(default=None, min_length=3, max_length=12)
    lat: Decimal | None = Field(default=None, ge=-90, le=90)
    lng: Decimal | None = Field(default=None, ge=-180, le=180)
    #: Editable, because a property genuinely can change hands or change use —
    #: and because every property that existed before the type column did is
    #: labelled a rental whether or not it is one. Leaving it off the update
    #: shape meant the form sent it, pydantic dropped it, and the endpoint
    #: answered 200 with the old value: a save that reports success and
    #: changes nothing, which is the worst way for this to fail.
    property_type: PropertyType | None = None

    bedrooms: int | None = Field(default=None, ge=0, le=50)
    bathrooms: Decimal | None = Field(default=None, ge=0, le=50)
    #: Same story. An owner who looks it up and types it in must not be told it
    #: was saved when it was thrown away.
    square_feet: int | None = Field(default=None, gt=0, le=100_000)
    default_checkout_time: time | None = None
    default_checkin_time: time | None = None
    access_notes: str | None = None
    cleaning_notes: str | None = None
    is_active: bool | None = None

    #: Columns the database will not accept a null in. Every field on a PATCH
    #: shape is optional so that omitting it means "leave it alone" — but that
    #: same `| None` makes an *explicit* null look like a valid value, and
    #: `exclude_unset` keeps it, so the route assigns None and Postgres rejects
    #: it as a 500. Omission and erasure are different requests and only one of
    #: them is allowed here.
    #:
    #: `square_feet`, `lat`, `lng`, `address_line2`, `access_notes` and
    #: `cleaning_notes` are deliberately absent: those columns *are* nullable,
    #: so clearing them is a real thing an owner may want to do — an owner who
    #: guessed wrong at the square footage can take it back out.
    NEVER_NULL: ClassVar[tuple[str, ...]] = (
        "nickname",
        "address_line1",
        "city",
        "state",
        "postal_code",
        "property_type",
        "bedrooms",
        "bathrooms",
        "default_checkout_time",
        "default_checkin_time",
        "is_active",
    )

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_nulls(cls, data: object) -> object:
        """A null on a non-nullable column is a 422, not a 500."""
        if isinstance(data, dict):
            for field in cls.NEVER_NULL:
                if field in data and data[field] is None:
                    raise ValueError(
                        f"{field} cannot be null — leave it out to keep it unchanged"
                    )
        return data

    @field_validator("state")
    @classmethod
    def uppercase_state(cls, v: str | None) -> str | None:
        return None if v is None else v.strip().upper()

    @field_validator("bathrooms")
    @classmethod
    def half_baths_only(cls, v: Decimal | None) -> Decimal | None:
        if v is not None and (v * 2) % 1 != 0:
            raise ValueError("bathrooms must be a whole or half number")
        return v


class PropertyOut(PropertyBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    owner_id: uuid.UUID
    is_active: bool
    created_at: datetime
    updated_at: datetime
