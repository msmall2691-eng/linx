"""Admin console shapes — the queue, the alarm, and the money.

Everything here is **admin-only and mostly read-only**. The console's job is to
show one person what needs a person, which means the shapes are built for
scanning a list rather than for driving a workflow: no bulk actions, no
assignment, no filters that hide something by default.

The one action in it is the refund, and that lives on the existing payments
route rather than here — there is exactly one door to Stripe, and a second
refund endpoint on the console would be a second place that moves money.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.enums import PaymentStatus, TurnoverUrgency


class ConsoleSummaryOut(BaseModel):
    """What needs a person right now, as four numbers.

    The console leads with these because an admin's first question is never
    "show me everything" — it is "is anything on fire". A zero here should be
    trustworthy enough that seeing four of them means the inbox can wait.
    """

    #: Disputes not yet resolved. Both `open` and `acknowledged` count: somebody
    #: having read it does not mean it is settled.
    open_disputes: int
    #: Cleaners with a vetting step still pending a human.
    vetting_pending: int
    #: Open turnovers close enough to checkout that nobody taking them is a
    #: problem. The same definition the scheduled alarm uses.
    unclaimed_turnovers: int
    #: Payments whose collected total does not equal payout plus fee. **This is
    #: the number that should never be anything but zero** — a non-zero drift
    #: means money moved that this codebase cannot account for.
    payments_with_drift: int


class UnclaimedTurnoverOut(BaseModel):
    """An open job nobody has taken, with checkout coming up."""

    model_config = ConfigDict(from_attributes=True)

    turnover_id: uuid.UUID
    property_nickname: str
    property_city: str
    property_state: str
    checkout_at: datetime
    checkin_at: datetime | None
    urgency: TurnoverUrgency
    owner_budget_cents: int | None
    #: How many cleaners have bid. Zero and "several, none accepted" are very
    #: different problems — the first is a supply gap, the second is an owner
    #: who has not chosen.
    bid_count: int
    owner_name: str
    owner_email: str


class LedgerRowOut(BaseModel):
    """One turnover's money, end to end.

    Every field is **integer cents**, and the four money numbers are the
    reconciliation identity written out: `collected - paid_out - platform_fee`
    is `drift`, and it is zero or something is wrong.
    """

    model_config = ConfigDict(from_attributes=True)

    turnover_id: uuid.UUID
    property_nickname: str
    checkout_at: datetime
    cleaner_name: str
    owner_name: str

    status: PaymentStatus
    #: What the owner was charged, less anything refunded.
    collected_cents: int
    #: What the cleaner kept, less anything reversed.
    paid_out_cents: int
    #: The platform's share. Zero after a full refund, which gives it back.
    platform_fee_cents: int
    #: `collected - paid_out - platform_fee`. **Alarm on anything but zero.**
    drift_cents: int

    refunded_amount_cents: int
    #: The reason a refund was issued, or why a payment failed.
    failure_message: str | None
    created_at: datetime


class LedgerOut(BaseModel):
    """The ledger page, plus the totals that have to add up.

    Totals are summed from the same rows rather than queried separately — two
    queries that could disagree about the same money is how a reconciliation
    screen ends up reassuring somebody about a number it did not check.
    """

    rows: list[LedgerRowOut]
    total_collected_cents: int
    total_paid_out_cents: int
    total_platform_fee_cents: int
    #: The sum of every row's drift. Zero, or somebody has work to do.
    total_drift_cents: int
