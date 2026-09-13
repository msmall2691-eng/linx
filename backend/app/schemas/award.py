"""Owner-facing award shapes: the bids on a job, and the booking that came out.

What an owner sees about a bidder is a deliberate list, same as the board's is
in the other direction. They are choosing who gets a key to their house, so they
get the cleaner's name, what they said, what they charge, and whether the
platform has cleared them — not the cleaner's address, documents, or anything
else on their profile.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import BidStatus


class BidderOut(BaseModel):
    """The cleaner behind a bid, as much as the owner needs to choose."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    full_name: str
    bio: str | None = None
    #: Straight from the vetting service, never re-derived here.
    can_take_jobs: bool
    #: A flag at v1, not a gate — shown prominently when it is missing.
    has_insurance_on_file: bool


class TurnoverBidOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    price_cents: int
    message: str | None
    status: BidStatus
    created_at: datetime
    updated_at: datetime
    cleaner: BidderOut


class AwardOut(BaseModel):
    """The booking. Carried on the turnover detail, so one request shows it."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    cleaner_id: uuid.UUID
    cleaner_name: str
    bid_id: uuid.UUID | None
    agreed_price_cents: int
    awarded_at: datetime
    cancelled_at: datetime | None
    cancellation_reason: str | None
    was_no_show: bool


class AwardCancel(BaseModel):
    """Ending a booking. A reason is required — somebody has to be told why."""

    reason: str = Field(min_length=1, max_length=2000)
