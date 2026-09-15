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
    #: Zero on an unsettled row rather than the cleaner's whole share, because
    #: nothing has been collected there to be out of balance with.
    drift_cents: int
    #: This row's intended amount while a checkout is still in flight. Never
    #: added to `collected_cents`; a payment is only true when Stripe says so.
    awaiting_cents: int = 0
    #: This row's amount when the outcome is unknown (`requires_review`).
    #: Neither collected nor written off — a person decides which.
    unknown_cents: int = 0

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
    #: Money a checkout is in the middle of collecting — an intention, not a
    #: fact. Deliberately outside the three totals above and outside drift: a
    #: payment is only true when Stripe says so, and an alarm that fires for
    #: every payment in flight is one nobody reads.
    total_awaiting_cents: int = 0
    #: Money whose fate nobody knows — guardrail 2's `requires_review`, written
    #: before the network call so a crash leaves a visible flag. Counted as
    #: neither collected nor lost, because an unknown outcome is neither.
    total_unknown_cents: int = 0


class LaunchCheckOut(BaseModel):
    """One readiness check, as the console renders it.

    `state` is rendered rather than re-decided: the frontend colours it and
    prints `detail` and `remedy` verbatim, for the same reason it renders
    `vetting` verbatim — a screen that re-derives a rule is a second author of
    it, and two authors disagree eventually.
    """

    key: str
    title: str
    #: ready | blocked | attention | unverifiable
    state: str
    detail: str
    remedy: str


class LaunchReadinessOut(BaseModel):
    """The whole list, plus the two counts a person reads first."""

    checks: list[LaunchCheckOut]
    #: Checked, and false. A pilot with real people must not start.
    blocking_count: int
    #: **Everything not finished, including what could not be checked.** An
    #: unverifiable item counts here, because a list that treats "I could not
    #: look" as "fine" reports all-green for a system nobody has confirmed.
    outstanding_count: int
    #: True only when nothing is outstanding at all.
    launchable: bool
