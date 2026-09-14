"""The bench board: open turnovers a cleaner can see and bid on.

**This is the privacy boundary of the product.** A cleaner browsing the board
has not been hired. They get what they need to price the job — size, timing,
urgency, roughly where it is — and nothing that would let them turn up at the
door.

Withheld on purpose, and each for a reason:

* `access_notes` — gate codes and lockbox locations. Released to the awarded
  cleaner when there is an award (phase 4), never to everyone who might bid.
* `address_line1` / `address_line2` — the exact street address. City and
  distance are enough to decide whether a job is worth taking.
* the owner's identity and contact details.

`BoardPropertyOut` exists as a separate model rather than a filtered
`PropertyOut` so that adding a field to the owner's shape cannot quietly widen
what cleaners see. A new field has to be added here deliberately.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import BidStatus, TurnoverStatus, TurnoverUrgency


class BoardPropertyOut(BaseModel):
    """What a bidding cleaner may know about a property. Nothing more."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    nickname: str
    city: str
    state: str
    postal_code: str
    bedrooms: int
    bathrooms: Decimal
    cleaning_notes: str | None


class BoardBidOut(BaseModel):
    """A cleaner's own bid. They never see anyone else's price."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    price_cents: int
    message: str | None
    status: BidStatus
    created_at: datetime
    updated_at: datetime


class BoardTurnoverOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    checkout_at: datetime
    checkin_at: datetime | None
    is_same_day: bool
    status: TurnoverStatus
    urgency: TurnoverUrgency
    owner_budget_cents: int | None
    notes: str | None
    created_at: datetime

    property: BoardPropertyOut
    #: Distance from the cleaner's service point, for sorting and display.
    distance_miles: float
    #: The caller's own bid, if they have already placed one.
    my_bid: BoardBidOut | None = None


class BidCreate(BaseModel):
    """A cleaner names a price."""

    price_cents: int = Field(gt=0, le=100_000_00, description="Integer cents. Never a float.")
    message: str | None = Field(default=None, max_length=2000)


class AwardedPropertyOut(BaseModel):
    """The other side of the boundary: this cleaner has actually been hired.

    A third model rather than a widened `BoardPropertyOut`, for the same reason
    that one is not a filtered `PropertyOut`. The street address and the gate
    code appear here because somebody has to open the door, and they appear
    *only* here — one shape a reader can check in full, rather than a flag
    somewhere that decides whether a field is filled in.

    Still withheld: the owner's identity and contact details. Phase 5's
    notifications are how the two sides reach each other.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    nickname: str
    address_line1: str
    address_line2: str | None
    city: str
    state: str
    postal_code: str
    bedrooms: int
    bathrooms: Decimal
    cleaning_notes: str | None
    #: Gate codes, lockbox locations. Released on award, never before.
    access_notes: str | None


class AwardedJobOut(BaseModel):
    """One job this cleaner is booked for.

    **One shape per resource**: every endpoint that answers with a single
    awarded job answers with this, the list included. An action that replied
    with less would blank the screen it replaced.
    """

    model_config = ConfigDict(from_attributes=True)

    award_id: uuid.UUID
    turnover_id: uuid.UUID
    checkout_at: datetime
    checkin_at: datetime | None
    is_same_day: bool
    status: TurnoverStatus
    urgency: TurnoverUrgency
    notes: str | None
    #: Integer cents, frozen when the bid was accepted.
    agreed_price_cents: int
    awarded_at: datetime
    #: The cleaner said they were on site, and that the job was done. The
    #: second one is what makes the turnover payable, so it is on the shape the
    #: cleaner's own screen reads rather than inferred from the status.
    started_at: datetime | None
    completed_at: datetime | None
    cancelled_at: datetime | None
    cancellation_reason: str | None
    was_no_show: bool

    property: AwardedPropertyOut


class JobCancel(BaseModel):
    """Backing out of a job. The reason is required and goes to the owner."""

    reason: str = Field(min_length=1, max_length=2000)
