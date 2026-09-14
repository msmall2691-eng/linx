"""Request and response shapes for turnovers.

`status`, `urgency`, and `is_same_day` are never accepted from a request.
Urgency is derived (see `app.services.urgency`) and status moves only through
the endpoints that own each transition — letting a client post either one would
put a second author on a field that is supposed to have exactly one.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import TurnoverStatus, TurnoverUrgency
from app.schemas.award import AwardOut
from app.schemas.property import PropertyOut


class TurnoverCreate(BaseModel):
    property_id: uuid.UUID
    checkout_at: datetime
    #: Null means a standing vacancy — no next guest booked yet.
    checkin_at: datetime | None = None
    owner_budget_cents: int | None = Field(default=None, ge=0)
    notes: str | None = None
    #: Post it to the bench immediately, or keep it as a draft.
    publish: bool = True

    @model_validator(mode="after")
    def checkin_after_checkout(self) -> "TurnoverCreate":
        if self.checkin_at is not None and self.checkin_at < self.checkout_at:
            raise ValueError("checkin_at cannot be before checkout_at")
        return self

    @model_validator(mode="after")
    def timestamps_must_be_timezone_aware(self) -> "TurnoverCreate":
        """Reject naive datetimes rather than guessing a timezone.

        A naive timestamp read as UTC silently moves a Maine checkout by four or
        five hours, which is the difference between a same-day turnover and an
        ordinary one.
        """
        for name in ("checkout_at", "checkin_at"):
            value = getattr(self, name)
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{name} must include a timezone offset")
        return self


class TurnoverUpdate(BaseModel):
    """Reschedule or re-describe a turnover. Status is not settable here."""

    checkout_at: datetime | None = None
    checkin_at: datetime | None = None
    owner_budget_cents: int | None = Field(default=None, ge=0)
    notes: str | None = None
    #: Explicit, because `checkin_at: None` in a PATCH body is ambiguous —
    #: "leave it alone" and "there is no next guest" look identical otherwise.
    clear_checkin: bool = False

    @model_validator(mode="after")
    def timestamps_must_be_timezone_aware(self) -> "TurnoverUpdate":
        for name in ("checkout_at", "checkin_at"):
            value = getattr(self, name)
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{name} must include a timezone offset")
        return self

    @model_validator(mode="after")
    def cannot_both_set_and_clear_checkin(self) -> "TurnoverUpdate":
        if self.clear_checkin and self.checkin_at is not None:
            raise ValueError("pass either checkin_at or clear_checkin, not both")
        return self


class TurnoverCancel(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)


class TurnoverOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    property_id: uuid.UUID
    checkout_at: datetime
    checkin_at: datetime | None
    is_same_day: bool
    status: TurnoverStatus
    urgency: TurnoverUrgency
    owner_budget_cents: int | None
    notes: str | None
    cancelled_at: datetime | None
    cancellation_reason: str | None
    #: Set when a booking came undone and the job went back on the bench.
    reopened_at: datetime | None
    created_at: datetime
    updated_at: datetime


class TurnoverDetailOut(TurnoverOut):
    """Detail view carries the property and the booking, so a screen needs one
    request — and so an action that answers with this shape cannot leave the
    screen holding less than the GET gave it."""

    property: PropertyOut
    #: The live award, read from `Turnover.live_award`: a cancelled booking is
    #: history, and a detail screen showing one as current would be a lie.
    award: AwardOut | None = Field(default=None, validation_alias="live_award")
