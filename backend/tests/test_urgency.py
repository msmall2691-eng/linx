"""The urgency ladder.

Urgency is the product's core pricing and priority signal, so the rule gets
tested directly at fixed instants rather than only through the endpoints. A
ladder whose tests drift with the time of day is a ladder nobody trusts.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.models.enums import TurnoverUrgency
from app.services.urgency import derive_urgency, is_same_day

EASTERN = ZoneInfo("America/New_York")


def _utc(year: int, month: int, day: int, hour: int = 12, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


class TestBookedCheckin:
    """With a next guest booked, the window between the two is the whole job."""

    def test_a_wide_window_is_standard(self) -> None:
        checkout = _utc(2026, 6, 1, 15)
        assert (
            derive_urgency(checkout, checkout + timedelta(days=5))
            is TurnoverUrgency.STANDARD
        )

    def test_a_two_day_window_is_soon(self) -> None:
        checkout = _utc(2026, 6, 1, 15)
        assert (
            derive_urgency(checkout, checkout + timedelta(hours=48))
            is TurnoverUrgency.SOON
        )

    def test_a_next_morning_window_is_urgent(self) -> None:
        checkout = _utc(2026, 6, 1, 20)  # 4pm Eastern
        checkin = _utc(2026, 6, 2, 19)  # 3pm Eastern next day, 23h later
        assert derive_urgency(checkout, checkin) is TurnoverUrgency.URGENT

    def test_same_calendar_day_is_the_top_of_the_ladder(self) -> None:
        checkout = _utc(2026, 6, 1, 15)  # 11am Eastern
        checkin = _utc(2026, 6, 1, 20)  # 4pm Eastern, same day
        assert derive_urgency(checkout, checkin) is TurnoverUrgency.SAME_DAY

    def test_same_day_outranks_a_comfortable_window(self) -> None:
        """A 10am checkout and a 10pm checkin is still same-day work."""
        checkout = _utc(2026, 6, 1, 14)  # 10am Eastern
        checkin = _utc(2026, 6, 2, 2)  # 10pm Eastern the same local day
        assert derive_urgency(checkout, checkin) is TurnoverUrgency.SAME_DAY

    def test_the_clock_does_not_change_a_booked_turnover(self) -> None:
        """A booked window is fixed. Only editing the dates moves it."""
        checkout = _utc(2026, 6, 1, 15)
        checkin = checkout + timedelta(days=5)
        far_future = _utc(2030, 1, 1)
        assert derive_urgency(checkout, checkin, now=far_future) is TurnoverUrgency.STANDARD


class TestStandingVacancy:
    """With no next guest, the signal is how soon checkout itself arrives."""

    def test_far_out_is_standard(self) -> None:
        now = _utc(2026, 6, 1)
        assert derive_urgency(now + timedelta(days=10), None, now=now) is TurnoverUrgency.STANDARD

    def test_two_days_out_is_soon(self) -> None:
        now = _utc(2026, 6, 1)
        assert derive_urgency(now + timedelta(days=2), None, now=now) is TurnoverUrgency.SOON

    def test_this_afternoon_is_urgent(self) -> None:
        now = _utc(2026, 6, 1)
        assert derive_urgency(now + timedelta(hours=4), None, now=now) is TurnoverUrgency.URGENT

    def test_a_checkout_already_past_is_urgent_not_standard(self) -> None:
        """The guest has left and nobody has cleaned it. That is the worst case.

        A negative gap must not fall through to `standard` by being compared as
        a magnitude — the whole point of the ladder is that this sits at the top.
        """
        now = _utc(2026, 6, 1)
        assert derive_urgency(now - timedelta(hours=6), None, now=now) is TurnoverUrgency.URGENT

    def test_a_vacancy_is_never_same_day(self) -> None:
        now = _utc(2026, 6, 1)
        assert derive_urgency(now + timedelta(hours=1), None, now=now) is not (
            TurnoverUrgency.SAME_DAY
        )


class TestSameDayIsAnsweredInLocalTime:
    """A region-local calendar day, not a UTC one."""

    def test_a_late_evening_checkin_is_still_the_same_local_day(self) -> None:
        """9pm Eastern on June 1 is June 2 in UTC — and still same-day here."""
        checkout = datetime(2026, 6, 1, 11, 0, tzinfo=EASTERN)
        checkin = datetime(2026, 6, 1, 21, 0, tzinfo=EASTERN)
        assert checkin.astimezone(timezone.utc).date() != checkout.astimezone(timezone.utc).date()
        assert is_same_day(checkout, checkin) is True

    def test_an_overnight_turnaround_is_not_same_day(self) -> None:
        checkout = datetime(2026, 6, 1, 23, 0, tzinfo=EASTERN)
        checkin = datetime(2026, 6, 2, 2, 0, tzinfo=EASTERN)
        assert is_same_day(checkout, checkin) is False
        # Three hours is still brutal, so it lands at the next rung down.
        assert derive_urgency(checkout, checkin) is TurnoverUrgency.URGENT

    def test_a_standing_vacancy_is_not_same_day(self) -> None:
        assert is_same_day(_utc(2026, 6, 1), None) is False

    def test_it_survives_a_dst_transition(self) -> None:
        """US DST ends Nov 1 2026: that local day is 25 hours long."""
        checkout = datetime(2026, 11, 1, 1, 0, tzinfo=EASTERN)
        checkin = datetime(2026, 11, 1, 22, 0, tzinfo=EASTERN)
        assert is_same_day(checkout, checkin) is True
        assert derive_urgency(checkout, checkin) is TurnoverUrgency.SAME_DAY


@pytest.mark.parametrize(
    ("hours", "expected"),
    [
        (23, TurnoverUrgency.URGENT),
        (24, TurnoverUrgency.SOON),
        (71, TurnoverUrgency.SOON),
        (72, TurnoverUrgency.STANDARD),
    ],
)
def test_the_rungs_sit_exactly_where_they_claim(hours: int, expected: TurnoverUrgency) -> None:
    """Pins the boundaries, so a later change to a threshold is a visible one."""
    now = _utc(2026, 6, 1)
    assert derive_urgency(now + timedelta(hours=hours), None, now=now) is expected
