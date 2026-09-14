"""Turnover helpers — keeping the derived fields honest.

`urgency` and `is_same_day` are stored columns, because the bench board sorts
and filters on them and a board cannot sort by a value computed in Python after
the query. Stored means they can go stale, so this module owns the one function
that writes them.

Staleness only affects standing vacancies. A turnover with a known checkin has
an urgency fixed by two immutable-until-edited timestamps; a vacancy's urgency
is measured against *now*, so it climbs the ladder as checkout approaches. That
is why the read paths call `refresh_urgency` and persist a change when they find
one: the alternative is a column that silently disagrees with what the same rule
would say today, which is the exact failure the single-source-of-truth rule
exists to prevent.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.enums import PropertyType, ServiceType, TurnoverStatus
from app.models.property import Property
from app.models.turnover import Turnover
from app.services.urgency import derive_urgency, is_same_day

#: How far past checkout an unclaimed turnover keeps alarming. Without a floor,
#: a first run against an old database would alert on every job the product
#: never had, and the one that matters would be buried in them.
UNCLAIMED_LOOKBACK = timedelta(days=1)


def apply_derived_fields(turnover: Turnover, *, now: datetime | None = None) -> bool:
    """Set `is_same_day` and `urgency` from the schedule.

    Returns True if either value changed, so callers can skip a pointless write.
    Call this after anything that touches `checkout_at` or `checkin_at`.
    """
    same_day = is_same_day(turnover.checkout_at, turnover.checkin_at)
    urgency = derive_urgency(
        turnover.checkout_at,
        turnover.checkin_at,
        reopened_at=turnover.reopened_at,
        now=now,
    )

    changed = turnover.is_same_day != same_day or turnover.urgency != urgency
    turnover.is_same_day = same_day
    turnover.urgency = urgency
    return changed


def refresh_urgency(
    db: Session,
    turnovers: list[Turnover],
    *,
    now: datetime | None = None,
) -> None:
    """Bring a batch of turnovers' derived fields up to date, committing once.

    Read paths call this so a standing vacancy that has climbed the ladder since
    it was last written is displayed — and sorted — at its real urgency.
    """
    if any([apply_derived_fields(t, now=now) for t in turnovers]):
        db.commit()


# --------------------------------------------------------------------------
# What kind of job fits what kind of property
# --------------------------------------------------------------------------


class JobRefused(Exception):
    """This job does not fit this property, and the reason is sayable."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


#: The scopes that belong to each kind of property. A rental's clean is the
#: turnaround between guests; a home's is a described piece of work.
SERVICE_TYPES_FOR = {
    PropertyType.SHORT_TERM_RENTAL: (ServiceType.TURNOVER,),
    PropertyType.RESIDENTIAL: (
        ServiceType.STANDARD,
        ServiceType.DEEP,
        ServiceType.MOVE_OUT,
    ),
}

#: What to assume when the owner does not say. Each property type has exactly
#: one obvious answer, which is why asking is optional.
DEFAULT_SERVICE_TYPE = {
    PropertyType.SHORT_TERM_RENTAL: ServiceType.TURNOVER,
    PropertyType.RESIDENTIAL: ServiceType.STANDARD,
}


def service_type_for(
    prop: Property, requested: ServiceType | None
) -> ServiceType:
    """The scope of work for a job at this property. **One author.**

    Refuses a mismatch rather than quietly correcting it: an owner who asked
    for a move-out clean and silently got a turnover would find out from the
    cleaner who turned up expecting two hours' work.
    """
    allowed = SERVICE_TYPES_FOR[prop.property_type]
    if requested is None:
        return DEFAULT_SERVICE_TYPE[prop.property_type]
    if requested not in allowed:
        names = ", ".join(option.value for option in allowed)
        raise JobRefused(
            f"A {prop.property_type.value.replace('_', ' ')} takes {names}, "
            f"not {requested.value}."
        )
    return requested


def checkin_for(prop: Property, checkin_at: datetime | None) -> datetime | None:
    """The next checkin, if this property has such a thing.

    **A home has no next guest**, so a checkin on one is not a slightly wrong
    value — it is a category error, and letting it through would put the job on
    the `same_day` rung of a ladder that measures the gap between bookings.
    Refused out loud rather than dropped, because an owner who typed a time
    into a field deserves to know it was ignored.
    """
    if checkin_at is None:
        return None
    if prop.property_type is PropertyType.RESIDENTIAL:
        raise JobRefused(
            "A home does not have a next guest checking in. Give the date and "
            "time the clean is due instead."
        )
    return checkin_at


def unclaimed_alarming(
    db: Session, *, now: datetime | None = None
) -> list[tuple[Turnover, Property]]:
    """Open turnovers close enough to checkout that nobody having taken them is
    a problem. **One definition, two readers.**

    The scheduled alarm (`app/tasks/scheduled.py`) and the admin console both
    ask this. They must not each carry their own version of "close enough": an
    admin screen that disagrees with the alert an admin was sent is worse than
    either alone, because it makes both untrustworthy and there is no way to
    tell which one is lying.

    Deliberately **not** a rung on the urgency ladder. Urgency is a property of
    the schedule and is what a cleaner sorts the board by; this is a question
    about *staffing* that only matters because a date is approaching, and it has
    its own cutoff and its own recipients (CLAUDE.md).
    """
    reference = now or datetime.now(timezone.utc)
    window_end = reference + timedelta(hours=settings.unclaimed_alert_hours_before)

    return list(
        db.execute(
            select(Turnover, Property)
            .join(Property, Turnover.property_id == Property.id)
            .where(
                Turnover.status == TurnoverStatus.OPEN,
                Turnover.checkout_at <= window_end,
                Turnover.checkout_at >= reference - UNCLAIMED_LOOKBACK,
            )
            .order_by(Turnover.checkout_at)
        ).all()
    )
