"""Shapes for calendar feeds.

**Owner-only, all of them.** A feed URL is secret in the way a link is secret:
anybody holding it can read the booking dates for somebody's house. It appears
on no cleaner-facing shape and no admin one, and the board schemas in
`app/schemas/board.py` are separate models precisely so a field added here
cannot leak into one by inheritance.
"""

from __future__ import annotations

import uuid
from datetime import datetime, time

from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CalendarCreate(BaseModel):
    """Point us at a listing's export link."""

    url: str = Field(min_length=10, max_length=2000)
    #: What the owner calls it. Free text rather than a platform guessed from
    #: the URL — a wrong label on somebody's own screen is worse than the one
    #: they typed.
    label: str = Field(default="Calendar", min_length=1, max_length=80)

    @field_validator("url")
    @classmethod
    def must_be_http(cls, v: str) -> str:
        """**Refuse anything that is not an http(s) URL.**

        This string becomes an outbound request from our server, so a `file://`
        or `http://169.254.169.254/...` here is somebody using the product to
        read things it can reach and they cannot. Scheme-checking is not the
        whole of SSRF defence, but accepting arbitrary schemes is not defensible
        at all.

        `webcal://` is the same feed with a different scheme — listing sites
        hand it out for one-click subscription — so it is rewritten rather than
        refused, which is what the owner meant.

        **The stored string is canonicalised, because uniqueness has to mean the
        same thing the network does.** `uq_property_calendars_property_url` is
        what stops one feed becoming two calendars — and therefore every booking
        becoming two drafts under two calendar ids — but it compares strings,
        not requests. Three ways the same request is spelled differently:

        * a **fragment**, which is never sent in an HTTP request at all;
        * the **scheme and host in different case**, which are case-insensitive;
        * an **explicit default port**, `:443` on https or `:80` on http.

        The path, query, and any credentials are left exactly as they are: those
        *are* case-sensitive, and a listing site's export links carry a token in
        one of them.
        """
        url = v.strip()
        if url.lower().startswith("webcal://"):
            url = "https://" + url[len("webcal://") :]
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("A calendar link must start with http:// or https://")

        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").lower()
        if not host:
            raise ValueError("That calendar link has no host in it.")

        # **An IPv6 literal keeps its brackets.** `parts.hostname` strips them,
        # and a bare `2606:4700:4700::1111` is not a host — re-parsing it reads
        # everything after the first colon as a port and raises, so the stored
        # URL could never be fetched again.
        if ":" in host:
            host = f"[{host}]"

        default_port = 443 if scheme == "https" else 80
        netloc = host if parts.port in (None, default_port) else f"{host}:{parts.port}"
        if parts.username:
            credentials = parts.username
            if parts.password:
                credentials = f"{credentials}:{parts.password}"
            netloc = f"{credentials}@{netloc}"

        # An empty path and "/" are the same request target, so they must be
        # the same string — otherwise `https://host` and `https://host/` are two
        # calendars pointing at one feed, and every booking becomes two drafts.
        path = parts.path or "/"

        # Fragment dropped by never putting it back.
        url = urlunsplit((scheme, netloc, path, parts.query, ""))
        if len(url) < 10:
            raise ValueError("That does not look like a calendar link.")
        return url


class CalendarUpdate(BaseModel):
    """Rename it or switch it off. The URL is not editable.

    Changing the URL in place would keep the calendar's id while pointing it at
    different bookings, and every turnover it had created would still be keyed
    to it — jobs from one listing claiming to come from another. Removing and
    re-adding is one more click and cannot produce that.
    """

    label: str | None = Field(default=None, min_length=1, max_length=80)
    is_active: bool | None = None


class CalendarOut(BaseModel):
    """A feed, and whether it is actually working."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    property_id: uuid.UUID
    url: str
    label: str
    is_active: bool

    last_synced_at: datetime | None
    #: Null when the last run worked. A sentence when it did not, written for
    #: the owner rather than for a log.
    last_error: str | None
    #: Bookings in the feed last time it was read. **Zero is a real answer and
    #: a suspicious one** — a number rather than a boolean so an owner can tell
    #: "nothing booked" from "this stopped working".
    last_booking_count: int | None
    #: Jobs whose booking has vanished but which somebody is already on. **The
    #: one number here that needs a person**, and the reason it is stored on the
    #: row rather than only returned by a sync: the pass that usually finds it
    #: is the scheduled one, which has no screen to answer.
    last_stale_kept: int | None

    created_at: datetime
    updated_at: datetime


class SyncOut(BaseModel):
    """What one sync did, in numbers an owner can act on."""

    calendar: CalendarOut
    bookings_seen: int
    created: int
    updated: int
    removed: int
    #: Drafts whose booking is gone that were **not** removed, because somebody
    #: had already posted or awarded them. The one number that needs a person:
    #: a guest cancelled and a cleaner may still be coming.
    stale_but_kept: int


class CalendarFileJob(BaseModel):
    """One clean an uploaded calendar implies, before anything is written."""

    checkout_at: datetime
    checkin_at: datetime | None


class CalendarFileJobsOut(BaseModel):
    """What an uploaded `.ics` says, for the bulk form to be filled in from.

    Carries `bookings_seen` as well as the jobs because the two differ for
    reasons an owner should see: stays outside the horizon or behind the past
    floor are read and then dropped, and a file of last year's bookings would
    otherwise come back as an unexplained empty list.
    """

    bookings_seen: int
    jobs: list[CalendarFileJob]


class DefaultTimesUpdate(BaseModel):
    """The house's own checkout and checkin policy.

    Separate from the feed because it is a property's fact, not a calendar's —
    and required by sync because an all-day export cannot supply it.
    """

    default_checkout_time: time | None = None
    default_checkin_time: time | None = None
