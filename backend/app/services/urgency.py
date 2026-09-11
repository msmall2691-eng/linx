"""The urgency ladder — the product's core pricing and priority signal.

`urgency` is derived from how close checkout is to the next checkin (or to now,
for a standing vacancy with no next guest booked). The closer, the more urgent.

**This module is the only place that decides.** Like `can_take_jobs`, urgency
is the kind of rule that goes wrong by being re-derived inline somewhere else —
a board that sorts by one definition and a badge that renders another, with no
way to tell which is lying. Every caller goes through `derive_urgency`.

Two separate measures, because a turnover means different things depending on
whether the next guest is booked:

* **Next checkin known** — the window between checkout and checkin is the whole
  job. A six-hour window is hard to staff no matter how far away it is.
* **Standing vacancy** (`checkin_at is None`) — there is no window, so the
  signal is how soon checkout itself arrives.

The "nobody has claimed this and checkout is tomorrow" alarm is deliberately
*not* here. That is an operational alert with its own cutoff and its own
recipients (owner and admin), and it belongs to the unclaimed-turnover path in
a later phase. Urgency stays a property of the schedule.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import settings
from app.models.enums import TurnoverUrgency

#: A window (or lead time) under this is `urgent`.
URGENT_WITHIN = timedelta(hours=24)
#: Under this, but not under URGENT_WITHIN, is `soon`.
SOON_WITHIN = timedelta(hours=72)


def region_timezone() -> ZoneInfo:
    """The one region's timezone.

    Single region at launch, so this is a setting rather than a per-property
    field. "Same day" has to be answered in local time: a 4pm checkout and a
    10pm checkin are the same day in Portland and two different days in UTC.
    """
    return ZoneInfo(settings.region_timezone)


def is_same_day(checkout_at: datetime, checkin_at: datetime | None) -> bool:
    """True when the next guest arrives the same calendar day the last one leaves.

    The hardest turnovers to staff, and the ones that price highest.
    """
    if checkin_at is None:
        return False
    tz = region_timezone()
    return checkout_at.astimezone(tz).date() == checkin_at.astimezone(tz).date()


def derive_urgency(
    checkout_at: datetime,
    checkin_at: datetime | None,
    *,
    now: datetime | None = None,
) -> TurnoverUrgency:
    """Place a turnover on the urgency ladder.

    `now` is injectable so the rule can be tested at a fixed instant rather than
    against the wall clock — a ladder whose tests drift with the time of day is
    a ladder nobody trusts.
    """
    if checkin_at is not None:
        if is_same_day(checkout_at, checkin_at):
            return TurnoverUrgency.SAME_DAY
        gap = checkin_at - checkout_at
    else:
        # Standing vacancy: measured from now to checkout. A checkout already in
        # the past yields a negative gap, which lands on `urgent` — correct, and
        # the reason the comparisons below are not written against abs().
        reference = now or datetime.now(tz=region_timezone())
        gap = checkout_at - reference

    if gap < URGENT_WITHIN:
        return TurnoverUrgency.URGENT
    if gap < SOON_WITHIN:
        return TurnoverUrgency.SOON
    return TurnoverUrgency.STANDARD
