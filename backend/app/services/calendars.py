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
import threading
import uuid
from dataclasses import dataclass
from hashlib import sha256
from datetime import date, datetime, time, timedelta, timezone
from time import monotonic
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

# **httpx logs the full request URL at INFO, and a feed URL is a credential.**
#
# `HTTP Request: GET https://www.airbnb.com/calendar/ical/123.ics?s=<token> "200 OK"`
# — that is httpx 0.28's own line, and listing sites put the token in the path
# or the query. The scheduled entry point turns the root logger up to INFO, so
# without this every unattended sync writes every owner's secret feed URL into
# the application log, where it is retained and readable by anyone with log
# access. The product treats that URL as secret everywhere else: it is on no
# cleaner-facing or admin shape, and there is a test that it appears nowhere in
# a board response. A log file is not an exception to that.
#
# **Module scope on purpose.** The leak happens in two processes — the web app
# for the Sync button, and the scheduled task — and this module is imported by
# both. Configuring it at each entry point instead would be two places to keep
# in step, which is exactly the shape of mistake this file has already made
# four times. The cost is httpx's request line for Stripe and Checkr too; both
# of those log their own outcomes, and neither line was load-bearing.
logging.getLogger("httpx").setLevel(logging.WARNING)

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

#: A hard ceiling on how long one feed may take in total.
#:
#: **`FETCH_TIMEOUT_SECONDS` does not bound this**, and the difference is the
#: whole point: httpx's read timeout is an inactivity timeout for each receive,
#: so a host dribbling one small chunk every nineteen seconds never trips it and
#: never reaches `MAX_FEED_BYTES` either. It can hold the connection for as long
#: as it likes.
#:
#: That matters here more than it would elsewhere, because the scheduled pass
#: reads feeds **serially and first** — ahead of the day-of reminders, the
#: unclaimed alarm, the review reveal and the outbox drain. One slow feed would
#: not just fail to sync; it would stop every one of those for every user, and
#: the review reveal is the one scheduled job this product cannot do without.
MAX_FEED_SECONDS = 60.0

#: How long to wait for a worker to notice its socket has been closed. Short,
#: because the read raises as soon as the close lands; this is only here so the
#: caller can tell whether the thread really ended.
CLOSE_GRACE_SECONDS = 5.0

#: How many feed reads may be in flight in this process at once.
#:
#: **A cap on the threads themselves, rather than another fix for one way they
#: can get stuck.** Closing the client ends a worker blocked on a socket, but it
#: cannot interrupt one still inside `socket.getaddrinfo` — a stalled resolver
#: is bounded by the OS (`/etc/resolv.conf` timeout × attempts × nameservers,
#: on the order of seconds) rather than unbounded like a header-trickle, but it
#: can still outlive `CLOSE_GRACE_SECONDS`. Rather than chase each way a worker
#: might outstay its deadline, this bounds the population: a permit is held by
#: the *thread*, not the caller, and released only when the thread truly ends.
#: Whatever the cause, threads and sockets cannot accumulate past this.
MAX_CONCURRENT_FETCHES = 4

#: How long a caller waits for a permit before giving up. Short: if every slot
#: is held, something is wrong and the next pass is a better time to try.
FETCH_SLOT_WAIT_SECONDS = 2.0

_FETCH_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_FETCHES)

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

#: The width of `Turnover.external_ref`. An identity that does not fit is
#: hashed rather than truncated — see `parse`.
MAX_EXTERNAL_REF = 500

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

#: Statuses where a booking disappearing is still the owner's problem: somebody
#: is lined up to clean a stay that is not happening. A `completed` or
#: `cancelled` job is not — the work is behind them either way.
STILL_NEEDS_A_DECISION = (
    TurnoverStatus.OPEN,
    TurnoverStatus.AWARDED,
    TurnoverStatus.IN_PROGRESS,
)

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

            # Counted as it arrives, and abandoned the moment it is too big
            # **or has taken too long**. Two separate limits because they catch
            # two different hosts: one that sends too much, and one that sends
            # too slowly to ever trip a per-read timeout. `iter_bytes` leaves
            # the rest of the body unread, and the `with` closes the connection.
            size = 0
            chunks: list[bytes] = []
            deadline = monotonic() + MAX_FEED_SECONDS
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_FEED_BYTES:
                    raise CalendarError(
                        "That link returned something far too large to be a "
                        "calendar — it is probably a web page rather than the "
                        ".ics export link."
                    )
                if monotonic() > deadline:
                    raise CalendarError(
                        "That calendar took too long to send. It is usually "
                        "temporary — the next sync will try again."
                    )
                chunks.append(chunk)

            # A charset name we do not have a codec for raises `LookupError`,
            # which is not an httpx error and so escaped every handler here —
            # a malformed reply from somebody else's server became a 500 with
            # no reason recorded, while every other unreadable feed is a
            # sentence on the owner's screen. UTF-8 is the honest fallback:
            # the file is text, and `errors="replace"` means a wrong guess
            # costs a mangled character rather than the whole sync.
            raw = b"".join(chunks)
            declared = response.charset_encoding or "utf-8"
            try:
                text = raw.decode(declared, errors="replace")
            except LookupError:
                logger.info("feed declared unknown charset %r; reading as utf-8", declared)
                text = raw.decode("utf-8", errors="replace")

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
        if value.tzinfo is not None:
            return value.astimezone(region_timezone()).date()
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
    seen: set[str] = set()
    for component in calendar.walk("VEVENT"):
        arrives = _as_date(getattr(component.get("DTSTART"), "dt", None))
        departs = _as_date(getattr(component.get("DTEND"), "dt", None))
        if arrives is None or departs is None or departs <= arrives:
            # A zero-length or backwards event describes no stay. Skipping it
            # is better than inventing a checkout from it.
            continue

        # A provider may keep a cancelled reservation in the feed rather than
        # dropping it. Treating it as a booking would create a draft for a stay
        # that is not happening — and worse, on later syncs it looks like a
        # live booking, so the job is never even reported as vanished.
        if str(component.get("STATUS") or "").strip().upper() == "CANCELLED":
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

        # **An occurrence is identified by UID *and* RECURRENCE-ID**, which is
        # iCalendar's own rule, not a workaround: a recurring event's overridden
        # instance legitimately repeats its parent's UID. Reading the UID alone
        # made two events one identity, and `(source_calendar_id, external_ref)`
        # is a unique constraint — so both rows were inserted and the *commit*
        # failed. Not a `CalendarError`, so it surfaced as a 500 with no reason
        # recorded anywhere, which is the one outcome this module is built to
        # avoid.
        # Formatted from the value, never `str()` of the library's object: that
        # is a repr which can change between versions, and identity that moves
        # would orphan every turnover keyed to the old spelling.
        occurrence = _as_date(getattr(component.get("RECURRENCE-ID"), "dt", None))
        identity = f"{uid}#{occurrence.isoformat()}" if occurrence else uid
        if len(identity) > MAX_EXTERNAL_REF:
            # **It has to fit `Turnover.external_ref`.** A UID near the column's
            # limit plus `#YYYY-MM-DD` runs past it, and the failure lands at
            # commit as a database error rather than a `CalendarError` — a 500
            # on the button and, on the scheduled pass, nothing the owner can
            # see. A digest is used rather than a truncation because two long
            # UIDs sharing a prefix would truncate to the same identity, which
            # is the one thing identity may never do.
            identity = "sha256:" + sha256(identity.encode()).hexdigest()

        if identity in seen:
            # Past that, a repeat is a feed we cannot interpret rather than two
            # stays. Keeping the first is a choice; inserting both is a crash.
            logger.info("calendar event %s appears more than once; keeping the first", identity)
            continue
        seen.add(identity)

        bookings.append(
            Booking(
                uid=identity, arrives_on=arrives, departs_on=departs, summary=summary
            )
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
            # Bounded at both ends. The lower bound is not theoretical: an
            # owner whose checkout time is later in the day than their checkin
            # time — a plausible thing to type — produces a *negative* window
            # on two stays that share a date, which a one-sided `<` accepts.
            # The row then violates the `checkin_after_checkout` constraint, so
            # connecting the feed 500s after the calendar has already been
            # saved and every later sync rolls back.
            if timedelta(0) <= candidate - checkout < NEXT_STAY_WITHIN:
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
    floor = moment - PAST_TOLERANCE
    for turnover in existing.values():
        if not _belongs_to_the_feed(turnover):
            # **A guest cancelling does not cancel a cleaner.** Reported so the
            # owner can decide, never removed underneath them.
            #
            # Only while there is still a decision to make, though. A finished
            # or cancelled job has no booking in the feed either — and neither
            # does one whose checkout has passed, because `jobs_for` stopped
            # proposing it. Counting those would make the warning climb by one
            # every sync forever, until "a guest cancelled and a cleaner may
            # still be coming" was mostly describing last month's completed
            # work. A warning that is usually wrong is one nobody reads.
            if turnover.status in STILL_NEEDS_A_DECISION and turnover.checkout_at >= floor:
                result.stale_but_kept += 1
            continue
        db.delete(turnover)
        result.removed += 1

    return result


# --------------------------------------------------------------------------
# One pass
# --------------------------------------------------------------------------


def refuse_ineligible(prop: Property) -> None:
    """A feed only makes sense on a live short-term rental.

    **The one author of that rule**, so the scheduled pass, the Sync button and
    the add endpoint cannot answer it differently. `active_calendars` carries
    the same condition in SQL purely so the pass does not fetch feeds it would
    then refuse; this is what decides.
    """
    if not prop.is_active:
        raise CalendarError(
            "This property is archived, so its calendar is not being read. "
            "Restore the property to start syncing again."
        )
    if prop.property_type is not PropertyType.SHORT_TERM_RENTAL:
        raise CalendarError(
            "A booking calendar describes guests checking out, which a home "
            "does not have. Remove the calendar, or set this back to a "
            "short-term rental."
        )


def _gate(calendar: PropertyCalendar, prop: Property) -> None:
    """Whether this feed may be read at all, over both rows at once.

    **One predicate, called twice**: cheaply before the network, and again on
    freshly locked rows before anything is written. That shape is the fix for a
    whole family of findings this module kept producing one at a time — a rule
    enforced on the scheduled query but not the button, or checked before the
    fetch but not after, or applied to the property but not to the calendar. If
    the answer can change, it has to be asked at both moments; if it is asked
    at both moments, it has to be the same question.
    """
    # Two different facts, refused separately because the owner's way out of
    # each is different: a paused feed is turned back on, a removed one is
    # reconnected. One message covering both would be wrong for whoever is
    # reading it.
    if calendar.removed_at is not None:
        raise CalendarError(
            "This calendar was removed. Add the same address again to "
            "reconnect it — the jobs it already proposed are still yours."
        )
    if not calendar.is_active:
        raise CalendarError(
            "This calendar is switched off. Turn it back on to read it again."
        )
    refuse_ineligible(prop)


def _claim(
    db: Session, calendar_id: uuid.UUID, property_id: uuid.UUID
) -> tuple[PropertyCalendar | None, Property | None]:
    """Lock both rows and re-read them, so what is checked is what is written.

    Two things, and missing either one loses the point:

    * **The lock**, held to the commit, so a settings change cannot land between
      the check and the write. `update_property` takes the same row lock.
    * **`populate_existing`**, because a locking SELECT still hands back the
      instance already in the identity map *with its old attribute values*. The
      row would be locked and then read stale, which is the entire failure.
      Verified by doing it both ways against a row changed in another session.

    Property first, then calendar, always — a consistent order is what stops
    two paths that take both locks from deadlocking against each other.
    """
    prop = db.execute(
        select(Property)
        .where(Property.id == property_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    calendar = db.execute(
        select(PropertyCalendar)
        .where(PropertyCalendar.id == calendar_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    return calendar, prop


def _fetch_within(
    url: str, *, client: httpx.Client | None = None, seconds: float
) -> str:
    """`fetch`, with a deadline that ends the work rather than walking away.

    **`MAX_FEED_SECONDS` inside the streaming loop does not bound the call**, and
    the gap is the interesting part: `client.stream()` must receive the whole
    response *head* before the loop is ever reached, and httpx's read timeout is
    per-receive inactivity. A host trickling header bytes holds the call open
    for as long as it likes — tying up the request worker behind the Sync button
    and stalling the scheduled pass, whose own budget is only checked between
    feeds and so cannot interrupt this.

    A blocking socket read cannot be cancelled, so the work happens on a daemon
    thread. **Stopping waiting is not enough on its own, though**, and an
    earlier version of this stopped there and called the leftover thread an
    acceptable residual. It is not: the body deadline is never reached, so no
    timeout ever fires for that thread, and each retry — every scheduled pass,
    every press of the button — starts another one that also never ends. That is
    not one leaked socket, it is an unbounded leak with a scheduler feeding it.

    So the deadline **closes the client**, which closes the socket underneath the
    blocked read and makes it raise. Verified against a server that dribbles
    header bytes forever: the worker ends within milliseconds of the close.
    A caller that injected its own client keeps ownership of it and it is left
    alone — that path opens no real socket anyway.

    **And the population is capped regardless** — see `MAX_CONCURRENT_FETCHES`.
    Closing the client cannot interrupt a worker still inside
    `socket.getaddrinfo`, so rather than chase each way a worker might outstay
    its deadline, the number of live workers is bounded outright.
    """
    owned = client is None
    client = client or _default_client()
    outcome: dict[str, object] = {}

    def work() -> None:
        try:
            outcome["text"] = fetch(url, client=client)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
            outcome["error"] = exc
        finally:
            # **Released by the thread, not the caller** — the caller may well
            # have given up already, and what this counts is live workers.
            _FETCH_SLOTS.release()

    if not _FETCH_SLOTS.acquire(timeout=FETCH_SLOT_WAIT_SECONDS):
        if owned:
            client.close()
        raise CalendarError(
            "Too many calendars are being read at once just now. The next sync "
            "will try again."
        )

    worker = threading.Thread(target=work, daemon=True, name="linx-calendar-fetch")
    try:
        worker.start()
    except BaseException:
        _FETCH_SLOTS.release()
        if owned:
            client.close()
        raise
    worker.join(seconds)

    try:
        if worker.is_alive():
            if owned:
                # Pull the socket out from under the blocked read.
                client.close()
                worker.join(CLOSE_GRACE_SECONDS)
            raise CalendarError(
                "That calendar took too long to answer. It is usually temporary "
                "— the next sync will try again."
            )
        error = outcome.get("error")
        if error is not None:
            raise error  # type: ignore[misc]
        return str(outcome["text"])
    finally:
        if owned and not worker.is_alive():
            client.close()


def sync(
    db: Session,
    calendar: PropertyCalendar,
    *,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> SyncResult:
    """Fetch, parse, reconcile, and record what happened. **Commits.**

    The sequence is the design, and it is written out here because four review
    rounds produced findings that were all really one finding: *a step in the
    wrong place*.

    1. **Gate, before touching the network.** An archived property or a
       switched-off feed is refused without an outbound request, so pressing
       Sync on a property the product says it no longer reads does not sit
       through a network timeout first.
    2. **Fetch, under a deadline the caller can rely on** — see `_fetch_within`.
    3. **Claim: lock and re-read both rows, then gate again.** The answer can
       have changed while we were on the network, and this is the copy that gets
       written from.
    4. **Reconcile and record**, inside that lock.

    **Every `CalendarError` out of here leaves its reason on the row**, from any
    step, because one of them previously escaped the recording block and a
    caller swallowed it assuming a reason had been written — the owner got a
    success with no jobs and no explanation. A uniform handler is what makes
    that class impossible rather than fixed.

    Nothing about the turnovers changes on a failure: "the feed is empty" and
    "the feed did not load" must never look the same, because the first one
    legitimately deletes drafts.
    """
    moment = now or datetime.now(tz=region_timezone())
    # The epoch as it stood when this attempt started. Everything about
    # overlapping reads is decided by comparing this against the locked row.
    epoch = calendar.sync_epoch

    try:
        prop = db.get(Property, calendar.property_id)
        if prop is None:
            raise CalendarError("That property no longer exists.")
        _gate(calendar, prop)

        text = _fetch_within(calendar.url, client=client, seconds=MAX_FEED_SECONDS)
        bookings = parse(text)

        calendar_locked, prop = _claim(db, calendar.id, calendar.property_id)
        if calendar_locked is None:
            raise CalendarError("That calendar has been removed.")
        if prop is None:
            raise CalendarError("That property no longer exists.")
        calendar = calendar_locked
        _gate(calendar, prop)

        # **The lock serialises the writes; it does not make this snapshot
        # current.** A manual sync and the scheduled pass can overlap, and the
        # slower fetch finishes second holding *older* bookings — reconciling
        # those would recreate a draft the newer pass correctly removed, or put
        # moved dates back. Whoever read the feed most recently wins, which is
        # the only ordering that means anything here.
        # **Has anything happened since we looked?** Under the lock, and as an
        # equality rather than a comparison of two clocks — see
        # `PropertyCalendar.sync_epoch`. If another read has succeeded while we
        # were on the network, our bookings are old news, and applying them
        # would recreate a draft it correctly removed or put moved dates back.
        if calendar.sync_epoch != epoch:
            logger.info(
                "calendar %s: discarding a snapshot from epoch %d, now at %d",
                calendar.id,
                epoch,
                calendar.sync_epoch,
            )
            db.commit()
            return SyncResult()

        result = reconcile(
            db, calendar, jobs_for(bookings, prop, now=moment), now=moment
        )

        calendar.last_error = None
        calendar.last_synced_at = moment
        # Bumped under the same lock that just verified it, which is what makes
        # the check above mean anything.
        calendar.sync_epoch = epoch + 1
        calendar.last_booking_count = len(bookings)
        # **Persisted, not just returned.** The scheduled pass is the one that
        # usually finds this, and it has nobody to hand a return value to — see
        # `PropertyCalendar.last_stale_kept`.
        calendar.last_stale_kept = result.stale_but_kept
        db.commit()
    except CalendarError as error:
        _record_failure(db, calendar.id, error, moment, epoch)
        raise

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


def _record_failure(
    db: Session,
    calendar_id: uuid.UUID,
    error: CalendarError,
    moment: datetime,
    epoch: int,
) -> None:
    """Leave the reason where the owner will see it, and change nothing else.

    **Rolls back first**, which matters now that this covers the reconcile step
    too: a failure partway through must not commit half a sync alongside its own
    error message.

    `epoch` is what the feed's `sync_epoch` was when this attempt started, and
    **the row is locked before it is compared**. Checking on an unlocked read
    and then writing is check-then-write — the shape guardrail 1 exists to
    forbid — and a successful sync committing in that gap would be buried by
    this failure.

    **That lock has no test, and it is worth saying so rather than implying
    otherwise.** Three attempts at one all passed with the lock removed: the
    re-read is not what it buys (the `rollback` above already expires the
    session), and the `UPDATE` blocks on a held row either way, so the
    observable outcome came out the same each time. The lock is kept because
    check-then-write is wrong regardless of whether this environment can be
    made to show it — but the epoch comparison below is what the tests cover.
    """
    db.rollback()
    calendar = db.execute(
        select(PropertyCalendar)
        .where(PropertyCalendar.id == calendar_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if calendar is None:
        # The feed was deleted underneath us. Nothing to write the reason on,
        # and nothing that needs it.
        db.commit()
        return
    if calendar.sync_epoch != epoch:
        # **An older failure does not get to bury a newer success.** A read has
        # succeeded since this one started, so writing this error now would put
        # a stale "could not be read" on a calendar that is currently fine, and
        # send the owner looking for a problem already over.
        logger.info(
            "calendar %s: not recording a failure from epoch %d, now at %d",
            calendar_id,
            epoch,
            calendar.sync_epoch,
        )
        db.commit()
        return
    calendar.last_error = error.detail
    calendar.last_synced_at = moment
    db.commit()


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
                PropertyCalendar.removed_at.is_(None),
                # The same rule `_refuse_ineligible` enforces, expressed in SQL
                # so the pass does not fetch feeds it would then refuse. **That
                # function is the authority**; this is an optimisation, and if
                # the two ever disagree the one in `sync` is the one that
                # decides, because every caller goes through it.
                Property.is_active.is_(True),
                Property.property_type == PropertyType.SHORT_TERM_RENTAL,
            )
            .order_by(PropertyCalendar.last_synced_at.asc().nulls_first())
        )
        .scalars()
        .all()
    )


def for_property(db: Session, property_id: uuid.UUID) -> list[PropertyCalendar]:
    """The feeds on this property's panel.

    Archived ones are kept forever, because the jobs they proposed still point
    at them, but they are **not** on the panel: to the owner they are removed,
    and a list that showed them would make "Remove" look like it had not
    worked.
    """
    return list(
        db.execute(
            select(PropertyCalendar)
            .where(
                PropertyCalendar.property_id == property_id,
                PropertyCalendar.removed_at.is_(None),
            )
            .order_by(PropertyCalendar.created_at)
        )
        .scalars()
        .all()
    )


def archive(db: Session, calendar: PropertyCalendar) -> None:
    """Remove a feed without destroying what its jobs are keyed to.

    Idempotent on the marker: re-archiving keeps the first timestamp, since
    "when was this removed" has one true answer and a second DELETE should not
    rewrite it.

    It is deliberately **not** paused as well. `is_active` is a separate fact
    the owner set, and a feed reconnected later should come back in the state
    they left it in rather than silently switched off — which would look like
    the reconnect had failed.
    """
    if calendar.removed_at is None:
        calendar.removed_at = datetime.now(timezone.utc)
    db.commit()


def reconnect(
    db: Session, *, property_id: uuid.UUID, url: str, label: str
) -> PropertyCalendar | None:
    """Bring an archived feed back, or report that this is a real duplicate.

    Returns `None` when the row that the URL collided with is **live** — that
    is an owner adding the same feed twice, which is still refused.

    The row is locked before its state is read, because two adds of the same
    archived URL race otherwise: both see it archived, both reactivate, and the
    second overwrites the first's label. Guardrail 1's shape applied to a state
    transition, `populate_existing` included — a locking SELECT still hands
    back the instance already in the session's identity map with its old
    values, which is exactly the failure the lock was taken to prevent.
    """
    calendar = db.execute(
        select(PropertyCalendar)
        .where(
            PropertyCalendar.property_id == property_id,
            PropertyCalendar.url == url,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalars().first()

    if calendar is None or calendar.removed_at is None:
        db.rollback()
        return None

    calendar.removed_at = None
    calendar.label = label
    # A reconnect is a fresh start for the *reading*, not for the jobs: the
    # last error belonged to a feed nobody was watching, and showing it now
    # would report a problem that may well be over. The epoch is deliberately
    # left alone — it is a monotonic counter guarding concurrent reads, and
    # resetting it would let an in-flight stale snapshot look current.
    calendar.last_error = None
    db.commit()
    return calendar


def change_url(
    db: Session, *, calendar_id: uuid.UUID, property_id: uuid.UUID, url: str
) -> PropertyCalendar:
    """Point an existing feed somewhere else, keeping its jobs.

    **This is only safe because removal stopped destroying the row.** While a
    calendar was deleted on removal, the URL was the closest thing a job had to
    a stable source, so editing it in place would have left every turnover
    keyed to a calendar claiming a source it never came from. Identity is the
    row's own id now; the URL is merely where to look. A provider rotating an
    export link — the case that used to force a remove-and-re-add, and duplicate
    every booking in the process — is an edit.

    Three things happen together, and each of them is the answer to "what does
    this row still know that is now untrue?"

    1. **The epoch is bumped, not reset.** A sync already out on the network
       captured the old epoch and is holding bookings from the *old* address;
       committing those into this calendar would fill it with another
       listing's stays. Bumping is exactly what `sync_epoch` is for — "has
       anything happened since I looked?" — and a URL change is the largest
       thing that can happen to a feed. Resetting to zero would be the
       opposite: it could collide with an in-flight value and make a stale
       snapshot look current, which is the trap `reconnect` names too.
    2. **The last-read state is cleared.** `last_synced_at`, the booking count,
       the error and the stale count all describe the address that is no longer
       connected. Left in place the panel would report "Last read an hour ago ·
       14 bookings" about a feed nobody has ever read, which is the one thing
       those numbers exist to prevent.
    3. **Nothing happens to the turnovers.** The jobs the old address proposed
       are the owner's, exactly as they are after a removal. On the next sync
       the new feed will not have their events, so an untouched draft goes and
       a posted or awarded one is kept and counted in `last_stale_kept` — the
       ordinary vanishing-booking policy, which already says what to do when a
       job has no booking behind it any more. That is the honest outcome for a
       repoint at a genuinely different listing, and a no-op for a rotated URL
       on the same one, where the UIDs come back unchanged.

    Raises `CalendarError` rather than returning a sentinel, so the reasons a
    repoint is refused read the same way as every other refusal in this module.
    """
    calendar, prop = _claim(db, calendar_id, property_id)
    if calendar is None or prop is None or calendar.removed_at is not None:
        db.rollback()
        raise CalendarError("That calendar is not connected to this property.")

    # **`refuse_ineligible`, not `_gate`.** They answer different questions:
    # `_gate` is "may this feed be read", and a *paused* feed may not be read
    # while its address is still perfectly fine to correct — an owner who
    # switched a feed off to stop it erroring should be able to fix the link
    # before switching it back on. What the two share is asked from its one
    # author rather than copied, which is the rule that retired a whole family
    # of findings here.
    refuse_ineligible(prop)

    if calendar.url == url:
        # Not an error, and deliberately not a write either: an owner who
        # pressed save without changing anything must not lose their read
        # history to a no-op. Canonicalisation is what makes this comparison
        # mean "the same request" rather than "the same string".
        db.rollback()
        return calendar

    # The constraint is the invariant; this read is only for the sentence. A
    # concurrent add does not take the property lock, so it can still land
    # between here and the commit — which is why `IntegrityError` is caught in
    # the route rather than assumed impossible.
    rival = db.execute(
        select(PropertyCalendar).where(
            PropertyCalendar.property_id == property_id,
            PropertyCalendar.url == url,
            PropertyCalendar.id != calendar_id,
        )
    ).scalars().first()
    if rival is not None:
        db.rollback()
        if rival.removed_at is not None:
            raise CalendarError(
                "That address belongs to a calendar you removed. Add it again "
                "to reconnect that one — its jobs are still yours."
            )
        raise CalendarError(
            "That address is already connected to this property."
        )

    calendar.url = url
    calendar.sync_epoch = calendar.sync_epoch + 1
    calendar.last_synced_at = None
    calendar.last_error = None
    calendar.last_booking_count = None
    calendar.last_stale_kept = None
    db.commit()
    db.refresh(calendar)
    return calendar
