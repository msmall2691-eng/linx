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
from app.schemas.review import ReputationOut


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
    #: What owners have said about them, from **visible** reviews only. Display
    #: only: the list is still sorted cheapest-first, because rating-weighted
    #: ranking is out of scope for v1 and a cleaner with no reviews yet must not
    #: be pushed below one with a single three-star.
    reputation: ReputationOut


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
    #: The cleaner said they were on their way. **The owner's answer to "is
    #: anybody coming"**, which the other two timestamps cannot give: they
    #: report what has already happened.
    en_route_at: datetime | None
    #: The cleaner said they were on site, and that it was done. The second is
    #: what makes the turnover payable, so the owner's screen reads it here
    #: rather than inferring it from the status.
    started_at: datetime | None
    #: `confirmed`, `away` or `unchecked` — see `awards.arrival_check`, which
    #: is the only thing that decides it.
    arrival_check: str
    arrival_distance_m: int | None
    completed_at: datetime | None
    cancelled_at: datetime | None
    cancellation_reason: str | None
    was_no_show: bool


class ArrivalIn(BaseModel):
    """One reading from the cleaner's browser, at the moment they tapped.

    **Every field is optional and the whole body may be omitted.** A refused
    permission, a phone with no signal and a desktop browser are all ordinary
    ways to arrive, and a shape that required a position would turn the check
    into a gate on being able to say you had turned up at all.

    The coordinates are used to compute one distance and are never stored.
    """

    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    #: What the fix claims about itself, in metres. Without it no conclusion is
    #: drawn — see `awards.arrival_check`, where a coarse fix that happens to
    #: land close by is `unchecked` rather than `confirmed`.
    accuracy_m: float | None = Field(default=None, ge=0)


class AwardCancel(BaseModel):
    """Ending a booking. A reason is required — somebody has to be told why."""

    reason: str = Field(min_length=1, max_length=2000)
