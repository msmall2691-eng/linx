"""Request and response shapes for properties.

`access_notes` holds gate codes, lockbox locations, and alarm instructions.
`PropertyOut` is an **owner-facing** shape and includes it. Any cleaner-facing
serializer added later must leave it out until that cleaner has actually been
awarded the turnover — a cleaner browsing the bench board has no business
holding the code to a house they have not been hired to clean.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PropertyBase(BaseModel):
    nickname: str = Field(min_length=1, max_length=120)
    address_line1: str = Field(min_length=1, max_length=200)
    address_line2: str | None = Field(default=None, max_length=200)
    city: str = Field(min_length=1, max_length=120)
    state: str = Field(min_length=2, max_length=2)
    postal_code: str = Field(min_length=3, max_length=12)

    lat: Decimal | None = Field(default=None, ge=-90, le=90)
    lng: Decimal | None = Field(default=None, ge=-180, le=180)

    bedrooms: int = Field(default=1, ge=0, le=50)
    bathrooms: Decimal = Field(default=Decimal("1.0"), ge=0, le=50)

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
    bedrooms: int | None = Field(default=None, ge=0, le=50)
    bathrooms: Decimal | None = Field(default=None, ge=0, le=50)
    access_notes: str | None = None
    cleaning_notes: str | None = None
    is_active: bool | None = None

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
