"""Reading a booking calendar, and turning it into drafts an owner confirms.

**A feed is a read-only projection of somebody else's system.** linx owns the
turnover; Airbnb owns the booking. Everything in this module follows from that
one sentence, and the rules it produces are worth stating before the code:

1. **A booking becomes a draft, never a live job.** An owner confirms it. A
   test booking or a feed glitch that posted straight to the bench would alert
   every cleaner in range, collect bids, and leave somebody apologising — and
   an owner who wanted ten jobs posted can do that in a minute, while an owner
   who did not cannot unsend the notifications.

2. **A row a person has touched is theirs.** Sync updates a draft it wrote and
   nothing else. `source_synced_at` against `updated_at` is the test: if the row
   changed after the last sync, somebody edited it, and the feed does not get to
   argue with them.

3. **Vanishing from the feed is not permission to delete.** A cancelled booking
   removes an *untouched draft*, because nothing was staffed for it. A job with
   a bid, an award or a posting on it is left exactly alone and reported —
   a guest cancelling does not get to cancel a cleaner.

4. **Identity is the event's UID, not its dates.** A booking whose dates move is
   the same booking; without that, it would become a second job while the first
   sat orphaned, and the owner would see two cleans for one stay.

5. **An unreadable feed changes nothing.** A timeout, a 404, an owner who
   pasted the wrong link: the error is recorded on the calendar row and every
   existing turnover is left alone. The failure mode of "the feed is empty" and
   "the feed did not load" must never be the same, because the first one
   legitimately deletes drafts.

**What an all-day calendar cannot tell us.** Airbnb and VRBO export whole days:
a guest leaves "on the 7th", with no hour. The urgency ladder is measured in
hours, so the property's own `default_checkout_time` / `default_checkin_time`
supply what the feed cannot. That is the house's policy, which the owner knows
and the calendar does not.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.calendar import PropertyCalendar
from app.models.enums import ServiceType, TurnoverStatus
from app.models.property import Property
from app.models.turnover import Turnover
from app.services.turnovers import apply_derived_fields
from app.services.urgency import region_timezone

logger = logging.getLogger("linx.calendars")

#: How long to wait on somebody else's server. Short on purpose: a scheduled
#: pass syncs every calendar in the product, and one slow host must not hold
#: up the rest.
FETCH_TIMEOUT_SECONDS = 20.0

#: A feed is a text file. A multi-megabyte response is a wrong URL — an HTML
#: page, a redirect loop, something that is not a calendar — and parsing it
#: would spend memory to reach the same conclusion.
MAX_FEED_BYTES = 4 * 1024 * 1024

#: Summaries that mean "the owner blocked these dates", not "a guest is
#: staying". Airbnb exports both through the same feed, and a block does not
#: need a clean at the end of it.
#:
#: **This is a heuristic and is written down as one.** It is matched
#: case-insensitively as a substring, it will not cover every platform, and the
#: failure mode is a draft turnover the owner deletes — which is why it is
#: allowed to be a heuristic at all. Nothing here is load-bearing enough to
#: justify pretending the categories are reliable.
BLOCK_MARKERS = (
    "not available",
    "unavailable",
    "blocked",
    "block",
    "owner stay",
    "maintenance",
)

#: How far ahead a booking is worth turning into a job. A calendar can contain
#: reservations a year out; a draft turnover that far ahead is noise on the
#: owner's screen long before it is useful, and the feed will still be there
#: when it gets closer.
HORIZON = timedelta(days=120)


class CalendarError(Exception):
    """The feed could not be read, and the reason is sayable to an owner."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class Booking:
    """One stay, as the feed describes it."""

    uid: str
    #: The day the guest arrives, and the day they leave. Whole days, because
    #: that is what the feed carries.
    arrives_on: date
    departs_on: date
    summary: str


@dataclass
class SyncResult:
    """What one pass over one calendar did. Reported, not just logged."""

    bookings_seen: int = 0
    created: int = 0
    updated: int = 0
    removed: int = 0
    #: Drafts the feed no longer has a booking for, which were **not** removed
    #: because somebody had already acted on them. The number an owner needs to
    #: see: a guest cancelled and a cleaner may still be coming.
    stale_but_kept: int = 0


# --------------------------------------------------------------------------
# Reading the feed
# --------------------------------------------------------------------------


def fetch(url: str, *, client: httpx.Client | None = None) -> str:
    """Get the raw .ics text. Raises `CalendarError` with a sayable reason.

    Every failure here is somebody else's server or somebody's typo, so none of
    them may look like a bug in this product on the owner's screen.
    """
    owned = client is None
    client = client or httpx.Client(timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=True)
    try:
        response = client.get(url)
        if response.status_code == 404:
            raise CalendarError(
                "That calendar link returned 'not found'. Listing sites change "
                "these when a listing is unpublished — copy the export link "
                "again from the listing."
            )
        if response.status_code >= 400:
            raise CalendarError(
                f"The calendar link answered {response.status_code}. It may have "
                "expired, or it may not be the export link."
            )
        if len(response.content) > MAX_FEED_BYTES:
            raise CalendarError(
                "That link returned something far too large to be a calendar — "
                "it is probably a web page rather than the .ics export link."
            )
        text = response.text
        if "BEGIN:VCALENDAR" not in text:
            raise CalendarError(
                "That link does not look like a calendar export. On Airbnb it "
                "is under Availability → Sync calendars → Export."
            )
        return text
    except httpx.TimeoutException as exc:
        raise CalendarError(
            "The calendar took too long to answer. It is usually temporary — "
            "the next sync will try again."
        ) from exc
    except httpx.HTTPError as exc:
        raise CalendarError(
            "Could not reach that calendar link. Check it is the export URL and "
            "still published."
        ) from exc
    finally:
        if owned:
            client.close()


def _as_date(value: object) -> date | None:
    """A DTSTART/DTEND as a plain day.

    Feeds carry either a DATE (whole day, which is what Airbnb sends) or a
    DATETIME. Both are reduced to a day here because the *time* comes from the
    property's policy, not the calendar — see the module docstring.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def parse(text: str) -> list[Booking]:
    """Bookings in the feed, blocks and junk left out.

    Deliberately forgiving about individual events and strict about the file: a
    calendar with one malformed event should still produce the other twenty,
    but a file that is not a calendar at all is an error the owner must see.
    """
    from icalendar import Calendar

    try:
        calendar = Calendar.from_ical(text)
    except Exception as exc:  # noqa: BLE001 - the library raises several types
        raise CalendarError(
            "That calendar could not be read. If you pasted the link by hand, "
            "copy it again from the listing."
        ) from exc

    bookings: list[Booking] = []
    for component in calendar.walk("VEVENT"):
        arrives = _as_date(getattr(component.get("DTSTART"), "dt", None))
        departs = _as_date(getattr(component.get("DTEND"), "dt", None))
        if arrives is None or departs is None or departs <= arrives:
            # A zero-length or backwards event describes no stay. Skipping it
            # is better than inventing a checkout from it.
            continue

        summary = str(component.get("SUMMARY") or "").strip()
        if any(marker in summary.casefold() for marker in BLOCK_MARKERS):
            continue

        uid = str(component.get("UID") or "").strip()
        if not uid:
            # Without an id there is no stable identity, so every sync would
            # create the booking again. Better to skip it than to duplicate it
            # every fifteen minutes.
            continue

        bookings.append(
            Booking(uid=uid, arrives_on=arrives, departs_on=departs, summary=summary)
        )

    bookings.sort(key=lambda b: b.arrives_on)
    return bookings


# --------------------------------------------------------------------------
# Turning bookings into jobs
# --------------------------------------------------------------------------


def _at(day: date, clock: time) -> datetime:
    """A whole day plus a policy time, read in the region's own zone.

    Never UTC. A checkout is 11am where the house is, and the urgency ladder
    answers "same day" in region-local time for the same reason.
    """
    return datetime.combine(day, clock, tzinfo=region_timezone())


@dataclass(frozen=True)
class ProposedJob:
    """A turnover a booking implies, before anything is written."""

    external_ref: str
    checkout_at: datetime
    checkin_at: datetime | None


def jobs_for(
    bookings: list[Booking], prop: Property, *, now: datetime | None = None
) -> list[ProposedJob]:
    """What cleans this calendar implies. **One per departure.**

    The next booking's arrival becomes the checkin *only when it is the very
    next thing on the calendar* — that gap is the job, and it is what the
    urgency ladder measures. A stay three weeks later is not a next checkin;
    it is a different week, and treating it as one would make every turnover
    look comfortable.
    """
    reference = now or datetime.now(tz=region_timezone())
    horizon = reference + HORIZON

    proposed: list[ProposedJob] = []
    for index, booking in enumerate(bookings):
        checkout = _at(booking.departs_on, prop.default_checkout_time)
        if checkout > horizon:
            continue

        checkin: datetime | None = None
        following = bookings[index + 1] if index + 1 < len(bookings) else None
        if following is not None and following.arrives_on >= booking.departs_on:
            checkin = _at(following.arrives_on, prop.default_checkin_time)

        proposed.append(
            ProposedJob(
                external_ref=booking.uid, checkout_at=checkout, checkin_at=checkin
            )
        )
    return proposed


def _touched_by_a_person(turnover: Turnover) -> bool:
    """Whether somebody has edited this since sync last wrote it.

    A row sync has never written is not ours to change either — that is a job
    the owner posted by hand, which happens to sit on a property with a feed.

    **Both timestamps come from the same clock**, and that is what makes the
    comparison exact. `updated_at` is written by Postgres with `now()`, which is
    transaction-start time, so sync sets `source_synced_at` to `now()` as well:
    a row sync wrote has the two equal to the microsecond, and any later
    transaction that touches it moves `updated_at` past it.

    An earlier version compared with a second of slack to absorb the difference
    between Python's clock and the database's. That was wrong in the direction
    that matters — an owner who edits a draft within a second of a sync would
    have had their edit silently reverted by the next one.
    """
    if turnover.source_synced_at is None:
        return True
    return turnover.updated_at > turnover.source_synced_at


def reconcile(
    db: Session,
    calendar: PropertyCalendar,
    proposed: list[ProposedJob],
    *,
    now: datetime | None = None,
) -> SyncResult:
    """Make the drafts match the feed, without ever overruling a person.

    **Does not commit.** The caller owns the transaction, so a partial sync
    cannot leave half a calendar applied.
    """
    moment = now or datetime.now(tz=region_timezone())
    result = SyncResult(bookings_seen=len(proposed))

    prop = db.get(Property, calendar.property_id)
    if prop is None:
        return result

    existing = {
        turnover.external_ref: turnover
        for turnover in db.execute(
            select(Turnover).where(Turnover.source_calendar_id == calendar.id)
        ).scalars()
        if turnover.external_ref is not None
    }

    for job in proposed:
        turnover = existing.pop(job.external_ref, None)

        if turnover is None:
            turnover = Turnover(
                property_id=prop.id,
                checkout_at=job.checkout_at,
                checkin_at=job.checkin_at,
                # A rental's clean is a turnover; a feed only ever describes a
                # rental, because a home does not have guests checking out.
                service_type=ServiceType.TURNOVER,
                # **Draft, always.** See rule 1 in the module docstring.
                status=TurnoverStatus.DRAFT,
                source_calendar_id=calendar.id,
                external_ref=job.external_ref,
                # The database's clock, not ours — see `_touched_by_a_person`.
                source_synced_at=func.now(),
            )
            apply_derived_fields(turnover, now=moment)
            db.add(turnover)
            result.created += 1
            continue

        if _touched_by_a_person(turnover):
            # Theirs now. The dates may have moved in the feed and we still do
            # not get to move them back.
            continue

        if (
            turnover.checkout_at == job.checkout_at
            and turnover.checkin_at == job.checkin_at
        ):
            continue

        turnover.checkout_at = job.checkout_at
        turnover.checkin_at = job.checkin_at
        turnover.source_synced_at = func.now()
        apply_derived_fields(turnover, now=moment)
        result.updated += 1

    # Whatever is left had a booking and does not any more.
    for turnover in existing.values():
        if turnover.status is not TurnoverStatus.DRAFT or _touched_by_a_person(turnover):
            # **A guest cancelling does not cancel a cleaner.** Reported so the
            # owner can decide, never removed underneath them.
            result.stale_but_kept += 1
            continue
        db.delete(turnover)
        result.removed += 1

    return result


# --------------------------------------------------------------------------
# One pass
# --------------------------------------------------------------------------


def sync(
    db: Session,
    calendar: PropertyCalendar,
    *,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> SyncResult:
    """Fetch, parse, reconcile, and record what happened. **Commits.**

    A failure is written to the calendar row and re-raised: the owner needs to
    see it on their own screen, and the caller needs to know not to count this
    as a successful pass. Crucially, **nothing about the turnovers changes** —
    "the feed is empty" and "the feed did not load" must never look the same,
    because the first one legitimately deletes drafts.
    """
    moment = now or datetime.now(tz=region_timezone())

    try:
        text = fetch(calendar.url, client=client)
        bookings = parse(text)
    except CalendarError as error:
        calendar.last_error = error.detail
        calendar.last_synced_at = moment
        db.commit()
        raise

    prop = db.get(Property, calendar.property_id)
    if prop is None:
        raise CalendarError("That property no longer exists.")

    result = reconcile(db, calendar, jobs_for(bookings, prop, now=moment), now=moment)

    calendar.last_error = None
    calendar.last_synced_at = moment
    calendar.last_booking_count = len(bookings)
    db.commit()

    logger.info(
        "calendar %s: %d bookings, %d created, %d updated, %d removed, %d kept",
        calendar.id,
        result.bookings_seen,
        result.created,
        result.updated,
        result.removed,
        result.stale_but_kept,
    )
    return result


def active_calendars(db: Session) -> list[PropertyCalendar]:
    """Every feed that should be polled, oldest sync first.

    Oldest first so a long list is drained fairly rather than the same few
    being refreshed while the tail goes stale.
    """
    return list(
        db.execute(
            select(PropertyCalendar)
            .where(PropertyCalendar.is_active.is_(True))
            .order_by(PropertyCalendar.last_synced_at.asc().nulls_first())
        )
        .scalars()
        .all()
    )


def for_property(db: Session, property_id: uuid.UUID) -> list[PropertyCalendar]:
    return list(
        db.execute(
            select(PropertyCalendar)
            .where(PropertyCalendar.property_id == property_id)
            .order_by(PropertyCalendar.created_at)
        )
        .scalars()
        .all()
    )
