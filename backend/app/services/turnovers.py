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

from dataclasses import dataclass, field
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


# --------------------------------------------------------------------------
# Several jobs at once, for an owner with no booking feed
#
# A calendar feed is the fast path onto the board, and plenty of owners cannot
# use it: a home has no booking calendar at all — that is what a home *is* —
# and a rental booked direct or by phone has no `.ics` URL to paste. Those
# owners were left typing one job per screen, which is fine for one and absurd
# for a season.
#
# This is deliberately **not** a second calendar source. It writes no
# `property_calendars` row, claims no `external_ref`, and is never reconciled
# against anything later: the owner said these dates once, and from then on the
# rows are ordinary turnovers they own. Everything in `calendars.py` about
# identity, adoption and vanishing bookings exists because a feed keeps
# *talking*; a list somebody typed does not, and borrowing that machinery would
# have meant maintaining rules with nothing behind them.
# --------------------------------------------------------------------------

#: The most jobs one submission may create.
#:
#: A bulk form is where a paste goes wrong, and the failure is not a big
#: request — it is an owner who meant six jobs and posted six hundred. The cap
#: is the difference between a mistake somebody notices and one they cannot
#: undo by hand.
MAX_BULK_JOBS = 100


@dataclass(frozen=True)
class BulkJob:
    """One job in a bulk submission, before anything is written."""

    checkout_at: datetime
    checkin_at: datetime | None = None


@dataclass
class BulkResult:
    """What one bulk submission did, reported rather than merely returned."""

    created: list[Turnover] = field(default_factory=list)
    #: Rows whose checkout already exists on this property, skipped and
    #: **named**. A duplicate is not a mismatch to refuse — the owner asked for
    #: a job that is already there — but silently dropping it is how somebody
    #: pastes twice and never learns the second one did nothing.
    already_there: list[datetime] = field(default_factory=list)


def _existing_checkouts(db: Session, prop: Property) -> set[datetime]:
    """Checkouts this property already has a job for.

    Cancelled ones are deliberately excluded: an owner who called a job off and
    is now re-entering that date means it, and refusing them the date they
    just freed up would be the system arguing with them about their own
    calendar.
    """
    rows = db.execute(
        select(Turnover.checkout_at).where(
            Turnover.property_id == prop.id,
            Turnover.status != TurnoverStatus.CANCELLED,
        )
    ).scalars().all()
    return set(rows)


def create_many(
    db: Session,
    *,
    prop: Property,
    jobs: list[BulkJob],
    service_type: ServiceType | None = None,
    owner_budget_cents: int | None = None,
    notes: str | None = None,
) -> BulkResult:
    """Create several drafts on one property, all or nothing.

    **Drafts, never posted, and not a setting.** This is calendars rule 1 in
    its other spelling: an owner who wanted ten jobs on the bench can post them
    in a minute, and an owner who did not cannot unsend the alerts, the bids or
    the apology. A bulk form is precisely where a wrong paste becomes twenty
    jobs, so the one irreversible step is the one it does not take. Drafts also
    notify nobody, which is why this adds no `NotificationEvent` — the closed
    list stays closed.

    **All or nothing.** A mismatch is refused rather than corrected, as
    everywhere else here, and for a list that has to mean the whole list: a
    partial write leaves the owner comparing what they pasted against what
    landed, with no way to retry that is not itself a duplicate.

    The scope and the checkin are decided by `service_type_for` and
    `checkin_for` — the same two functions the single-job endpoint asks — so a
    home still refuses a checkin here, and refuses it per row with the row
    named.
    """
    if not jobs:
        raise JobRefused("No dates were given.")
    if len(jobs) > MAX_BULK_JOBS:
        raise JobRefused(
            f"{len(jobs)} jobs at once is more than the {MAX_BULK_JOBS} this "
            "takes. Add them in smaller batches, so a mistake stays small too."
        )

    # One author for both rules, asked rather than restated. `service_type_for`
    # is asked once because the scope is shared across the submission;
    # `checkin_for` is asked per row because each row carries its own.
    scope = service_type_for(prop, service_type)

    seen: set[datetime] = set()
    existing = _existing_checkouts(db, prop)
    result = BulkResult()
    pending: list[Turnover] = []

    for index, job in enumerate(jobs, start=1):
        try:
            checkin_at = checkin_for(prop, job.checkin_at)
        except JobRefused as refused:
            raise JobRefused(f"Row {index}: {refused.detail}") from None
        if checkin_at is not None and checkin_at < job.checkout_at:
            raise JobRefused(
                f"Row {index}: the checkin is before the checkout."
            )

        # A duplicate *inside* the submission is the same paste-twice mistake
        # as one against the database, and is reported the same way rather
        # than inserted twice.
        if job.checkout_at in existing or job.checkout_at in seen:
            result.already_there.append(job.checkout_at)
            continue
        seen.add(job.checkout_at)

        turnover = Turnover(
            property_id=prop.id,
            checkout_at=job.checkout_at,
            checkin_at=checkin_at,
            service_type=scope,
            owner_budget_cents=owner_budget_cents,
            notes=notes,
            status=TurnoverStatus.DRAFT,
        )
        apply_derived_fields(turnover)
        pending.append(turnover)

    for turnover in pending:
        db.add(turnover)
    result.created = pending
    return result
