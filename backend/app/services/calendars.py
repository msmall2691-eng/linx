"""Reading a booking calendar, and turning it into drafts an owner confirms.

**A feed is a read-only projection of somebody else's system.** linx owns the
turnover; Airbnb owns the booking. Everything in this module follows from that
one sentence, and the rules it produces are worth stating before the code:

1. **A booking becomes a draft, never a live job.** An owner confirms it. A
   test booking or a feed glitch that posted straight to the bench would alert
   every cleaner in range, collect bids, and leave somebody apologising — and
   an owner who wanted ten jobs posted can do that in a minute, while an owner
   who did not cannot unsend the notifications.

2. **A row a person has touched is theirs.** Sync updates a *draft* it wrote and
   nothing else — `_belongs_to_the_feed` is both halves of that in one place.
   The person-test is an explicit `owner_edited_at`, set where people edit, and
   deliberately **not** `updated_at`: the read paths write too (`refresh_urgency`
   persists a standing vacancy's climb up the ladder), so inferring an edit from
   `updated_at` let a page view quietly take a draft away from the feed.

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

6. **A URL an owner types is a place this server connects to.** Every hop is
   checked and anything that is not a public address is refused
   (`_PublicOnlyTransport`), and the body is capped *as it streams*. Both of
   those are about what the field can be pointed at rather than what it usually
   holds: without them the export-link box is a request-forgery primitive and
   an unbounded download, on a path that runs unattended for every owner.

**What an all-day calendar cannot tell us.** Airbnb and VRBO export whole days:
a guest leaves "on the 7th", with no hour. The urgency ladder is measured in
hours, so the property's own `default_checkout_time` / `default_checkin_time`
supply what the feed cannot. That is the house's policy, which the owner knows
and the calendar does not.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from urllib.parse import urlparse

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.calendar import PropertyCalendar
from app.models.enums import PropertyType, ServiceType, TurnoverStatus
from app.models.property import Property
from app.models.turnover import Turnover
from app.services.turnovers import apply_derived_fields
from app.services.urgency import SOON_WITHIN, region_timezone

logger = logging.getLogger("linx.calendars")

#: How long to wait on somebody else's server. Short on purpose: a scheduled
#: pass syncs every calendar in the product, and one slow host must not hold
#: up the rest.
FETCH_TIMEOUT_SECONDS = 20.0

#: A feed is a text file. A multi-megabyte response is a wrong URL — an HTML
#: page, a redirect loop, something that is not a calendar — and parsing it
#: would spend memory to reach the same conclusion.
#:
#: **Enforced while the body streams in, never after it has arrived.** Checking
#: `len(response.content)` reads the whole thing first, which is not a limit at
#: all: a host that answers with an endless body would hold a worker and its
#: memory for as long as it cared to, on every scheduled pass.
MAX_FEED_BYTES = 4 * 1024 * 1024

#: A feed that needs more hops than this is not a feed. httpx's own default is
#: twenty, which is twenty chances for one of them to point somewhere private.
MAX_REDIRECTS = 5

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

#: How far *behind* now a departure can be and still be worth proposing.
#:
#: Exports keep their history — Airbnb's carries every past stay — and the
#: horizon above only rejects dates that are too far ahead. Without a floor as
#: well, connecting a calendar proposes a draft for every stay the listing has
#: ever had, each one overdue and therefore `urgent`, and the owner's first
#: experience of the feature is deleting a year of them.
#:
#: Not zero, because a checkout this morning is a job somebody may still need
#: doing — the ladder deliberately rates an already-past checkout `urgent`.
PAST_TOLERANCE = timedelta(days=1)

#: How close behind a departure the next arrival has to be to count as *this*
#: turnover's checkin.
#:
#: This is `SOON_WITHIN` rather than a number of its own, so the rule and the
#: ladder it exists to feed cannot drift apart. Past that gap the window says
#: nothing the lead time does not say better: a stay three weeks later would
#: make a checkout that is twelve hours away read `standard`, because the
#: measure would be the length of a three-week window rather than how soon the
#: job is.
NEXT_STAY_WITHIN = SOON_WITHIN


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


def _refuse_private_address(url: httpx.URL | str) -> None:
    """Refuse a URL that points anywhere but the public internet.

    **The server makes this request, so the owner is choosing where our process
    connects.** Without this, the export-link field is a request forgery
    primitive: `http://169.254.169.254/` reaches a cloud metadata service,
    `http://127.0.0.1:8000/` reaches this application's own unauthenticated
    surface, and `http://10.0.0.5/` reaches whatever else shares the network.

    Every name is resolved and **every** address it resolves to must be global.
    A host with one public and one loopback address is refused, because which
    one gets connected to is not ours to choose.

    **The residual gap is named rather than papered over.** Between this lookup
    and httpx's own, a hostile DNS server can answer differently — the classic
    rebind. Closing that means pinning the connection to the address checked
    here, which is a bigger change than this path warrants; what it buys over
    nothing at all is that the straightforward attack, and the honest mistake of
    pasting a `localhost` URL, are both refused.
    """
    parsed = urlparse(str(url))
    if parsed.scheme not in ("http", "https"):
        # `CalendarCreate` refuses these on the way in; a redirect is the other
        # way in, and it does not go through a schema.
        raise CalendarError("That calendar link is not a web address.")

    host = parsed.hostname
    if not host:
        raise CalendarError("That calendar link has no host in it.")

    try:
        infos = socket.getaddrinfo(
            host, parsed.port or (443 if parsed.scheme == "https" else 80),
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as exc:
        raise CalendarError(
            "Could not look up that calendar link's address. Check the link is "
            "the export URL and still published."
        ) from exc

    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise CalendarError(
                "That calendar link points inside a private network rather than "
                "at a listing site. Paste the export link from the listing."
            )


class _PublicOnlyTransport(httpx.HTTPTransport):
    """An HTTP transport that will not connect to a private address.

    The guard belongs **in the transport, not in `fetch`**, because every
    redirect hop passes through here. Checking only the URL the owner typed
    would be satisfied by a perfectly public host that answers `302` to
    `http://169.254.169.254/` — which is the whole trick, not an edge case.

    A test that injects its own client opens no socket at all, so there is
    nothing for this to guard; that is why it is wired into the client this
    module builds rather than into the function every caller shares.
    """

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        _refuse_private_address(request.url)
        return super().handle_request(request)


def _default_client() -> httpx.Client:
    return httpx.Client(
        timeout=FETCH_TIMEOUT_SECONDS,
        follow_redirects=True,
        max_redirects=MAX_REDIRECTS,
        transport=_PublicOnlyTransport(),
    )


def fetch(url: str, *, client: httpx.Client | None = None) -> str:
    """Get the raw .ics text. Raises `CalendarError` with a sayable reason.

    Every failure here is somebody else's server or somebody's typo, so none of
    them may look like a bug in this product on the owner's screen.

    The body is **streamed and capped as it arrives** — see `MAX_FEED_BYTES`.
    """
    owned = client is None
    client = client or _default_client()
    try:
        with client.stream("GET", url) as response:
            if response.status_code == 404:
                raise CalendarError(
                    "That calendar link returned 'not found'. Listing sites "
                    "change these when a listing is unpublished — copy the "
                    "export link again from the listing."
                )
            if response.status_code >= 400:
                raise CalendarError(
                    f"The calendar link answered {response.status_code}. It may "
                    "have expired, or it may not be the export link."
                )

            # Counted as it arrives, and abandoned the moment it is too big.
            # `iter_bytes` leaves the rest of the body unread, and the `with`
            # closes the connection on the way out.
            size = 0
            chunks: list[bytes] = []
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_FEED_BYTES:
                    raise CalendarError(
                        "That link returned something far too large to be a "
                        "calendar — it is probably a web page rather than the "
                        ".ics export link."
                    )
                chunks.append(chunk)

            text = b"".join(chunks).decode(
                response.charset_encoding or "utf-8", errors="replace"
            )

        if "BEGIN:VCALENDAR" not in text:
            raise CalendarError(
                "That link does not look like a calendar export. On Airbnb it "
                "is under Availability → Sync calendars → Export."
            )
        return text
    except httpx.TooManyRedirects as exc:
        raise CalendarError(
            "That calendar link redirects too many times to follow. Copy the "
            "export link again from the listing."
        ) from exc
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
    floor = reference - PAST_TOLERANCE

    proposed: list[ProposedJob] = []
    for index, booking in enumerate(bookings):
        checkout = _at(booking.departs_on, prop.default_checkout_time)
        # Bounded at both ends. The feed carries history as well as future, and
        # only one of those is a job somebody still needs — see `PAST_TOLERANCE`.
        if checkout > horizon or checkout < floor:
            continue

        checkin: datetime | None = None
        following = bookings[index + 1] if index + 1 < len(bookings) else None
        if following is not None and following.arrives_on >= booking.departs_on:
            candidate = _at(following.arrives_on, prop.default_checkin_time)
            # **Only when it is genuinely the next thing**, which this function
            # has always claimed and did not previously enforce. A stay weeks
            # later is a different week, and calling it this clean's checkin
            # measures the wrong thing — see `NEXT_STAY_WITHIN`.
            if candidate - checkout < NEXT_STAY_WITHIN:
                checkin = candidate

        proposed.append(
            ProposedJob(
                external_ref=booking.uid, checkout_at=checkout, checkin_at=checkin
            )
        )
    return proposed


def _touched_by_a_person(turnover: Turnover) -> bool:
    """Whether a person — not the system — has had a hand in this row.

    A row sync has never written is not ours to change either: that is a job the
    owner posted by hand, which happens to sit on a property with a feed.

    **`owner_edited_at` is an explicit marker, set only where a person edits.**
    An earlier version asked `updated_at > source_synced_at` instead, on the
    reasoning that both come from the database clock and are therefore exactly
    comparable. They are — and it was still wrong, because it answers a
    different question. `updated_at` moves for *any* write, and the read paths
    write: `refresh_urgency` persists a standing vacancy's climb up the urgency
    ladder, which is precisely the kind of row a synced draft with no next guest
    is. Loading the turnover list was enough to stamp a draft as edited, after
    which the feed could neither move its dates nor withdraw it when the guest
    cancelled — rule 2 switched off by a page view, with no test failing.
    """
    if turnover.source_synced_at is None:
        return True
    return turnover.owner_edited_at is not None


def _belongs_to_the_feed(turnover: Turnover) -> bool:
    """Whether sync may still change this row at all.

    Both halves, in one place, because the two used to be one by accident.
    While the person-test was `updated_at > source_synced_at`, publishing a
    draft satisfied it as a side effect — the status write moved `updated_at`.
    Swapping in an explicit edit marker removed that coincidence, and without
    this the feed would have gained the ability to move the dates of a job
    already on the bench with bids on it.

    So the status test is written down rather than inherited: **a draft, and
    only a draft, is the feed's to maintain.** That is what this module has
    claimed from the first line of its docstring.
    """
    return turnover.status is TurnoverStatus.DRAFT and not _touched_by_a_person(
        turnover
    )


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

    # **Locked before anything is checked about them**, and held to the commit.
    # This is guardrail 1's shape applied to a different pair of writers: sync
    # reads a row, decides from `owner_edited_at` that it is still the feed's,
    # and writes. An owner PATCH landing in that gap would be silently
    # overwritten by the write that follows a check made before it existed.
    #
    # `key_share=True` makes it `FOR NO KEY UPDATE`, which an owner's `UPDATE`
    # blocks on while still letting the foreign-key checks on bids and awards
    # take their `FOR KEY SHARE`. A bare `FOR UPDATE` conflicts with those and
    # would block on rows nobody is editing.
    existing = {
        turnover.external_ref: turnover
        for turnover in db.execute(
            select(Turnover)
            .where(Turnover.source_calendar_id == calendar.id)
            .with_for_update(key_share=True)
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

        if not _belongs_to_the_feed(turnover):
            # Theirs now — edited, or already posted to the bench. The dates may
            # have moved in the feed and we still do not get to move them.
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
        if not _belongs_to_the_feed(turnover):
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
    # **Persisted, not just returned.** The scheduled pass is the one that
    # usually finds this, and it has nobody to hand a return value to — see
    # `PropertyCalendar.last_stale_kept`.
    calendar.last_stale_kept = result.stale_but_kept
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
            .join(Property, Property.id == PropertyCalendar.property_id)
            .where(
                PropertyCalendar.is_active.is_(True),
                # **The property has to still be one a turnover fits.** A feed
                # only describes guests checking out, and `reconcile` writes
                # `service_type=turnover` — which is a category error on a home
                # and refused everywhere else in the product. An owner may
                # archive a property or reclassify it as residential once its
                # live work is finished, and the feed would otherwise keep
                # proposing rental jobs onto it, on a screen that no longer
                # even shows the calendar panel.
                Property.is_active.is_(True),
                Property.property_type == PropertyType.SHORT_TERM_RENTAL,
            )
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
