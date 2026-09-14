"""Shapes for disputes — what a person files, and what an admin works.

Two response models rather than one, and the split is the same reasoning the
bench board uses: **`DisputeOut` is what a party sees about their own dispute,
`AdminDisputeOut` is what an admin sees.** Making the second a subclass of the
first means a field added for the admin cannot quietly appear on the party's
shape — it has to be put there deliberately.

What an admin sees and a party does not is the **other side's identity**. The
owner's name and contact are withheld from cleaners everywhere else in the
product, and a dispute is not the place that leaks: a cleaner reading their own
complaint gets the property and the job, not the owner's details.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import DisputeReason, DisputeStatus, UserRole


class DisputeIn(BaseModel):
    """Filing one. A category for triage, and the description that matters."""

    reason: DisputeReason
    #: **Which booking the complaint is about.** Optional only because most
    #: turnovers have exactly one, and asking a question with one possible
    #: answer is noise. When there are several the server refuses rather than
    #: picking: a turnover that was cancelled and re-awarded has two cleaners
    #: in its history, and inferring the newest files the complaint against
    #: whoever holds the job today.
    award_id: uuid.UUID | None = None
    #: Required, and required to be non-empty after stripping: an empty dispute
    #: cannot be acted on, and a person who submits one has told nobody
    #: anything while believing they have.
    description: str = Field(min_length=1, max_length=8000)


class DisputeResolution(BaseModel):
    """Closing one. The note is the decision."""

    notes: str = Field(min_length=1, max_length=8000)


class DisputeOut(BaseModel):
    """A dispute as the person who raised it sees it."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    turnover_id: uuid.UUID
    #: Which side raised it — owner or cleaner. Not *who*: on a dispute the
    #: other side raised, the identity stays withheld the same as everywhere.
    raised_by_role: UserRole
    reason: DisputeReason
    description: str
    status: DisputeStatus
    #: When a person picked it up. Present so the queue is visibly not a void;
    #: it sends nothing on its own.
    acknowledged_at: datetime | None
    resolved_at: datetime | None
    #: What was decided, in the admin's words. Null until it is.
    resolution_notes: str | None
    created_at: datetime
    updated_at: datetime


class DisputePartyOut(BaseModel):
    """One side of a dispute, for the admin who has to contact them."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    full_name: str
    email: str
    phone: str | None


class AdminDisputeOut(DisputeOut):
    """The same dispute, plus what somebody working it needs.

    The extra fields are all about **reaching people and seeing the job** —
    admins are the one role that sees both sides, because settling a
    disagreement between two people you cannot contact is not possible.
    """

    #: Where and when, so the queue reads without opening every row.
    property_nickname: str
    property_city: str
    property_state: str
    checkout_at: datetime
    #: Both sides. This is the field that must never migrate to `DisputeOut`.
    owner: DisputePartyOut
    cleaner: DisputePartyOut
    raised_by: DisputePartyOut
    #: Whether the booking is still live, cancelled, or was a no-show — the
    #: context that usually explains the complaint before you read it.
    award_cancelled_at: datetime | None
    award_was_no_show: bool


class DisputableBookingOut(BaseModel):
    """One booking this person could file a complaint about.

    Carries only what is needed to tell two bookings apart — who was on it and
    what became of it. The cleaner's name is not a widening of the privacy
    boundary: for an owner these are the cleaners they themselves accepted, and
    for a cleaner every entry is their own award.
    """

    model_config = ConfigDict(from_attributes=True)

    award_id: uuid.UUID
    cleaner_name: str
    awarded_at: datetime
    cancelled_at: datetime | None
    was_no_show: bool


class DisputesOut(BaseModel):
    """Everything one person may see about a turnover's disputes.

    **One shape per resource**: the GET and the POST both answer with this, so a
    screen that replaces its state with an action's reply keeps everything it
    was showing. An action returning less than the GET is how a page goes blank
    on the render after a successful click — which has happened here before.

    `mine` is only ever the reader's own. A dispute the other side raised is not
    in it, and there is no count of them either: at v1 a human decides when
    somebody is told they are being complained about.
    """

    turnover_id: uuid.UUID
    #: Whether this person can file one right now.
    can_raise: bool
    #: Why not, in words, or null when they can. Rendered verbatim.
    blocker: str | None
    mine: list[DisputeOut]
    #: The bookings this person may complain about, newest first. Usually one.
    #: When it is more than one the screen has to ask which, because the server
    #: refuses to guess — see `disputes.award_under_dispute`.
    bookings: list[DisputableBookingOut] = []
