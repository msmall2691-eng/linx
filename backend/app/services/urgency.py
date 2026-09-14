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

* **Re-posted after a cancellation** (`reopened_at` set, phase 4) — the window
  still says what it said, but nobody is staffed for the job any more, so the
  lead time counts again exactly as it does for a vacancy. The more urgent of
  the two readings wins.

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


#: The ladder, least urgent first — the order the enum itself declares.
LADDER: tuple[TurnoverUrgency, ...] = tuple(TurnoverUrgency)


def _rung_for_gap(gap: timedelta) -> TurnoverUrgency:
    """Place a window — or a lead time — on the ladder.

    A negative gap (a checkout already in the past) lands on `urgent`, which is
    correct and the reason these comparisons are not written against abs().
    """
    if gap < URGENT_WITHIN:
        return TurnoverUrgency.URGENT
    if gap < SOON_WITHIN:
        return TurnoverUrgency.SOON
    return TurnoverUrgency.STANDARD


def _most_urgent(*rungs: TurnoverUrgency) -> TurnoverUrgency:
    return max(rungs, key=LADDER.index)


def derive_urgency(
    checkout_at: datetime,
    checkin_at: datetime | None,
    *,
    reopened_at: datetime | None = None,
    now: datetime | None = None,
) -> TurnoverUrgency:
    """Place a turnover on the urgency ladder.

    `reopened_at` is set when a booking came undone and the job went back on the
    bench (phase 4). It does not invent a second rule: it says that nobody is
    staffed for this job any more, so the time left to find somebody counts
    again — the same measure a standing vacancy has always used. Whichever of
    the two readings is more urgent wins, so a cancellation six hours before
    checkout is `urgent` even though the cleaning window itself is wide, and a
    cancellation three weeks out is still judged on the window.

    `now` is injectable so the rule can be tested at a fixed instant rather than
    against the wall clock — a ladder whose tests drift with the time of day is
    a ladder nobody trusts.
    """
    reference = now or datetime.now(tz=region_timezone())

    if checkin_at is not None:
        if is_same_day(checkout_at, checkin_at):
            # Already the top rung; nothing can raise it further.
            return TurnoverUrgency.SAME_DAY
        schedule = _rung_for_gap(checkin_at - checkout_at)
    else:
        # Standing vacancy: no window to measure, so the lead time is the whole
        # signal — which is also what a re-posted job is judged on.
        schedule = _rung_for_gap(checkout_at - reference)

    if reopened_at is None:
        return schedule
    return _most_urgent(schedule, _rung_for_gap(checkout_at - reference))
