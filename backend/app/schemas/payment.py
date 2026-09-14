"""Response shapes for the money endpoints.

Money is integer cents on the wire as well as in the database. A dollar figure
is a rendering decision the frontend makes at the last moment, never a number
this API sends — a float on the wire is a float that finds its way into a total.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import PaymentStatus


class ConnectStatusOut(BaseModel):
    """Whether money can reach this cleaner, and what is missing if not.

    Deliberately separate from the vetting shape. `can_take_jobs` answers "may
    this person be in a stranger's house" and has exactly one author; this
    answers "can a transfer land", which is Stripe's answer, not ours. A screen
    that ran them together would make a verification delay look like a vetting
    problem.
    """

    model_config = ConfigDict(from_attributes=True)

    connected: bool
    details_submitted: bool
    payouts_enabled: bool
    #: Plain-language reason, or null when money can reach them. Rendered
    #: verbatim, the same rule as the vetting reason.
    blocker: str | None
    #: False on a deployment with no Stripe key. The screen says "not set up
    #: yet" rather than showing a button that cannot work.
    payments_configured: bool


class OnboardingLinkOut(BaseModel):
    """A one-time URL into Stripe's hosted Express onboarding."""

    url: str


class PaymentOut(BaseModel):
    """What has been collected for one turnover, and what reached the cleaner.

    Null `status` means nothing has been charged yet — a turnover with no
    payment row is not an error, it is the ordinary state of every job before
    it is done.
    """

    model_config = ConfigDict(from_attributes=True)

    turnover_id: uuid.UUID
    status: PaymentStatus | None
    #: All integer cents. `amount == platform_fee + cleaner` always.
    amount_cents: int | None
    platform_fee_cents: int | None
    cleaner_cents: int | None
    refunded_amount_cents: int = 0
    #: What the cleaner actually kept, after any reversal.
    paid_out_cents: int | None
    #: Why a payment failed, or the reason a refund was issued. Shown to the
    #: owner, so it is a sentence rather than a Stripe error code.
    failure_message: str | None


class RefundRequest(BaseModel):
    """Refunds are a human decision and the reason is not optional.

    A refund reverses a transfer a cleaner has already been told they earned.
    Whoever issues one says why, in words, because that sentence is what the
    dispute is argued from later.
    """

    reason: str = Field(min_length=3, max_length=1000)
