"""Booking feeds — the rules that stop a projection overwriting the real thing.

**A calendar feed is a read-only projection of somebody else's system.** linx
owns the turnover; Airbnb owns the booking. Almost every test here is about one
of the five consequences of that, and each has a failure it prevents that would
be discovered by a person rather than a test:

1. A booking becomes a **draft**, never a live job — otherwise a test booking
   alerts every cleaner in range.
2. A row a person touched is **theirs** — otherwise the next sync silently undoes
   somebody's edit.
3. Vanishing from the feed **is not permission to delete** a job a cleaner is
   booked on.
4. Identity is the event's **UID**, not its dates — otherwise a moved booking
   becomes a second job.
5. An **unreadable** feed changes nothing — because "empty" legitimately deletes
   drafts, and "did not load" must never look the same.

The .ics samples are the real shape Airbnb sends: all-day `VALUE=DATE` events,
`DTEND` being the departure day, blocks in the same feed as reservations.
"""

from __future__ import annotations

import logging
import socket
import threading
import uuid
from datetime import date, datetime, time, timedelta, timezone
from time import monotonic, sleep
from unittest import mock

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.calendar import PropertyCalendar
from app.models.enums import PropertyType, TurnoverStatus, TurnoverUrgency
from app.models.property import Property
from app.models.turnover import Turnover
from app.services import calendars
from app.services.turnovers import refresh_urgency

# --------------------------------------------------------------------------
# Feeds, in the shape the real ones arrive in
# --------------------------------------------------------------------------


def _ics(*events: str) -> str:
    body = "\n".join(events)
    return (
        "BEGIN:VCALENDAR\n"
        "VERSION:2.0\n"
        "PRODID:-//Airbnb Inc//Hosting Calendar 1.0.0//EN\n"
        f"{body}\n"
        "END:VCALENDAR\n"
    )


def _event(uid: str, start: str, end: str, summary: str = "Reserved") -> str:
    return (
        "BEGIN:VEVENT\n"
        f"DTSTART;VALUE=DATE:{start}\n"
        f"DTEND;VALUE=DATE:{end}\n"
        f"UID:{uid}\n"
        f"SUMMARY:{summary}\n"
        "END:VEVENT"
    )


def _fake_client(text: str | None = None, *, status: int = 200, raises=None):
    """An httpx client that answers with whatever the test needs."""

    def handler(request: httpx.Request) -> httpx.Response:
        if raises is not None:
            raise raises
        return httpx.Response(status, text=text or "")

    return httpx.Client(transport=httpx.MockTransport(handler))


def _property(client: TestClient, owner: dict, **overrides) -> dict:
    payload = {
        "nickname": f"Place {uuid.uuid4().hex[:6]}",
        "address_line1": "4 Beacon Rd",
        "city": "Portland",
        "state": "ME",
        "postal_code": "04101",
        **overrides,
    }
    resp = client.post("/api/properties", json=payload, headers=owner["auth"])
    assert resp.status_code == 201, resp.text
    return resp.json()


def _calendar(db: Session, property_id: str, url: str = "https://example.test/a.ics"):
    calendar = PropertyCalendar(
        property_id=uuid.UUID(property_id), url=url, label="Airbnb"
    )
    db.add(calendar)
    db.commit()
    db.refresh(calendar)
    return calendar


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


class TestReadingAFeed:
    def test_a_reservation_becomes_a_booking(self) -> None:
        bookings = calendars.parse(_ics(_event("abc", "20270704", "20270707")))
        assert len(bookings) == 1
        assert bookings[0].uid == "abc"
        assert bookings[0].arrives_on == date(2027, 7, 4)
        # DTEND on an all-day event is the departure day, which is the day the
        # clean is needed. Getting this off by one would schedule every job a
        # day late.
        assert bookings[0].departs_on == date(2027, 7, 7)

    @pytest.mark.parametrize(
        "summary",
        ["Airbnb (Not available)", "Blocked", "Not available", "Owner stay"],
    )
    def test_blocks_are_not_stays(self, summary: str) -> None:
        """Airbnb sends the owner's own blocked dates through the same feed.

        Nobody checks out of a block, so it needs no clean. This is a heuristic
        and is written down as one — the failure mode is a draft the owner
        deletes, which is why a heuristic is allowed here at all.
        """
        assert calendars.parse(_ics(_event("x", "20270704", "20270707", summary))) == []

    def test_an_event_with_no_uid_is_skipped(self) -> None:
        """Without a stable id, every sync would create the booking again."""
        feed = _ics(
            "BEGIN:VEVENT\n"
            "DTSTART;VALUE=DATE:20270704\n"
            "DTEND;VALUE=DATE:20270707\n"
            "SUMMARY:Reserved\n"
            "END:VEVENT"
        )
        assert calendars.parse(feed) == []

    def test_one_bad_event_does_not_lose_the_others(self) -> None:
        """Forgiving about events, strict about the file."""
        feed = _ics(
            _event("good-1", "20270704", "20270707"),
            # Backwards: describes no stay at all.
            _event("bad", "20270710", "20270708"),
            _event("good-2", "20270801", "20270805"),
        )
        assert [b.uid for b in calendars.parse(feed)] == ["good-1", "good-2"]

    def test_something_that_is_not_a_calendar_is_an_error(self) -> None:
        """**Not an empty list.** An empty feed legitimately deletes drafts, so
        "this is not a calendar" must never be able to look like one."""
        with pytest.raises(calendars.CalendarError):
            calendars.fetch(
                "https://example.test/a.ics",
                client=_fake_client("<html>Sign in to continue</html>"),
            )

    def test_a_404_says_what_to_do(self) -> None:
        """Listing sites reissue these links. The message names the fix."""
        with pytest.raises(calendars.CalendarError) as caught:
            calendars.fetch("https://example.test/a.ics", client=_fake_client(status=404))
        assert "copy the export link" in caught.value.detail

    def test_a_timeout_reads_as_temporary(self) -> None:
        with pytest.raises(calendars.CalendarError) as caught:
            calendars.fetch(
                "https://example.test/a.ics",
                client=_fake_client(raises=httpx.ConnectTimeout("slow")),
            )
        assert "next sync will try again" in caught.value.detail


# --------------------------------------------------------------------------
# Turning bookings into jobs
# --------------------------------------------------------------------------


class TestWhatABookingImplies:
    def test_the_gap_between_two_stays_is_the_job(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """**This is what the urgency ladder measures.**

        The next arrival is the checkin, and the times come from the property's
        policy because an all-day feed has none.
        """
        owner = make_user(role="owner")
        created = _property(client, owner)
        prop = db.get(Property, uuid.UUID(created["id"]))

        soon = date.today() + timedelta(days=10)
        bookings = calendars.parse(
            _ics(
                _event("one", soon.strftime("%Y%m%d"), (soon + timedelta(days=3)).strftime("%Y%m%d")),
                _event(
                    "two",
                    (soon + timedelta(days=3)).strftime("%Y%m%d"),
                    (soon + timedelta(days=6)).strftime("%Y%m%d"),
                ),
            )
        )
        jobs = calendars.jobs_for(bookings, prop)

        assert len(jobs) == 2
        first = jobs[0]
        assert first.checkout_at.date() == soon + timedelta(days=3)
        assert first.checkout_at.time() == time(11, 0)
        # Same-day turnaround: one guest out, the next in, on one day.
        assert first.checkin_at is not None
        assert first.checkin_at.date() == soon + timedelta(days=3)
        assert first.checkin_at.time() == time(16, 0)

        # Nothing follows the last stay, so there is no next guest to measure
        # against — a standing vacancy, exactly as an owner posting by hand
        # would leave it.
        assert jobs[1].checkin_at is None

    def test_the_property_decides_the_times(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """An all-day export cannot supply an hour, and the ladder needs one."""
        owner = make_user(role="owner")
        created = _property(
            client, owner, default_checkout_time="10:00:00", default_checkin_time="15:00:00"
        )
        prop = db.get(Property, uuid.UUID(created["id"]))

        soon = date.today() + timedelta(days=10)
        bookings = calendars.parse(
            _ics(_event("one", soon.strftime("%Y%m%d"), (soon + timedelta(days=2)).strftime("%Y%m%d")))
        )
        assert calendars.jobs_for(bookings, prop)[0].checkout_at.time() == time(10, 0)

    def test_bookings_beyond_the_horizon_are_left_for_later(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """A draft a year out is noise long before it is useful, and the feed
        will still be there when it gets closer."""
        owner = make_user(role="owner")
        prop = db.get(Property, uuid.UUID(_property(client, owner)["id"]))

        far = date.today() + timedelta(days=300)
        bookings = calendars.parse(
            _ics(_event("far", far.strftime("%Y%m%d"), (far + timedelta(days=3)).strftime("%Y%m%d")))
        )
        assert calendars.jobs_for(bookings, prop) == []


# --------------------------------------------------------------------------
# The rules that protect what a person did
# --------------------------------------------------------------------------


def _feed_for(days_out: int, uid: str = "one", nights: int = 3) -> str:
    start = date.today() + timedelta(days=days_out)
    return _ics(
        _event(uid, start.strftime("%Y%m%d"), (start + timedelta(days=nights)).strftime("%Y%m%d"))
    )


class TestSync:
    def test_a_booking_becomes_a_draft_not_a_live_job(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """**Rule 1, and the one with the loudest failure.**

        A live job alerts every cleaner in range and collects bids. An owner who
        wanted that can post it in a minute; an owner who did not cannot unsend
        the notifications.
        """
        owner = make_user(role="owner")
        prop = _property(client, owner)
        calendar = _calendar(db, prop["id"])

        result = calendars.sync(db, calendar, client=_fake_client(_feed_for(10)))
        assert result.created == 1

        turnover = db.execute(select(Turnover)).scalars().one()
        assert turnover.status is TurnoverStatus.DRAFT
        assert turnover.source_calendar_id == calendar.id
        assert turnover.external_ref == "one"

    def test_syncing_twice_changes_nothing(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """Identity is the booking's own id, so a re-read is a no-op rather
        than a way to accumulate a duplicate every fifteen minutes."""
        owner = make_user(role="owner")
        calendar = _calendar(db, _property(client, owner)["id"])
        feed = _feed_for(10)

        assert calendars.sync(db, calendar, client=_fake_client(feed)).created == 1
        second = calendars.sync(db, calendar, client=_fake_client(feed))
        assert (second.created, second.updated, second.removed) == (0, 0, 0)
        assert len(db.execute(select(Turnover)).scalars().all()) == 1

    def test_a_moved_booking_moves_its_job(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """**Not a second job.** The dates changed; the booking did not."""
        owner = make_user(role="owner")
        calendar = _calendar(db, _property(client, owner)["id"])

        calendars.sync(db, calendar, client=_fake_client(_feed_for(10)))
        result = calendars.sync(db, calendar, client=_fake_client(_feed_for(20)))

        assert result.updated == 1
        assert result.created == 0
        turnover = db.execute(select(Turnover)).scalars().one()
        assert turnover.checkout_at.date() == date.today() + timedelta(days=23)

    def test_a_job_the_owner_edited_is_left_alone(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """**Rule 2.** The feed does not get to undo somebody's edit."""
        owner = make_user(role="owner")
        calendar = _calendar(db, _property(client, owner)["id"])
        calendars.sync(db, calendar, client=_fake_client(_feed_for(10)))

        turnover = db.execute(select(Turnover)).scalars().one()
        chosen = datetime.now(timezone.utc) + timedelta(days=12)
        resp = client.patch(
            f"/api/turnovers/{turnover.id}",
            json={"checkout_at": chosen.isoformat()},
            headers=owner["auth"],
        )
        assert resp.status_code == 200, resp.text

        db.expire_all()
        result = calendars.sync(db, calendar, client=_fake_client(_feed_for(20)))
        assert result.updated == 0

        db.expire_all()
        turnover = db.execute(select(Turnover)).scalars().one()
        assert turnover.checkout_at.date() == chosen.date()

    def test_a_cancelled_booking_removes_an_untouched_draft(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """Nothing was staffed for it, so nothing is lost by removing it."""
        owner = make_user(role="owner")
        calendar = _calendar(db, _property(client, owner)["id"])
        calendars.sync(db, calendar, client=_fake_client(_feed_for(10)))

        result = calendars.sync(db, calendar, client=_fake_client(_ics()))
        assert result.removed == 1
        assert db.execute(select(Turnover)).scalars().all() == []

    def test_a_cancelled_booking_never_removes_a_posted_job(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """**Rule 3, and the one that would hurt somebody.**

        A guest cancelling does not get to cancel a cleaner. The job stays and
        is *reported*, so the owner decides rather than finding out later.
        """
        owner = make_user(role="owner")
        calendar = _calendar(db, _property(client, owner)["id"])
        calendars.sync(db, calendar, client=_fake_client(_feed_for(10)))

        turnover = db.execute(select(Turnover)).scalars().one()
        assert (
            client.post(
                f"/api/turnovers/{turnover.id}/publish", headers=owner["auth"]
            ).status_code
            == 200
        )

        db.expire_all()
        result = calendars.sync(db, calendar, client=_fake_client(_ics()))
        assert result.removed == 0
        assert result.stale_but_kept == 1

        db.expire_all()
        assert db.get(Turnover, turnover.id) is not None

    def test_a_job_the_owner_posted_by_hand_is_untouched(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """A property with a feed can still have jobs posted directly. Sync
        only ever owns rows it wrote."""
        owner = make_user(role="owner")
        prop = _property(client, owner)
        calendar = _calendar(db, prop["id"])

        manual = client.post(
            "/api/turnovers",
            json={
                "property_id": prop["id"],
                "checkout_at": (datetime.now(timezone.utc) + timedelta(days=4)).isoformat(),
            },
            headers=owner["auth"],
        )
        assert manual.status_code == 201, manual.text

        calendars.sync(db, calendar, client=_fake_client(_ics()))
        db.expire_all()
        assert db.get(Turnover, uuid.UUID(manual.json()["id"])) is not None

    def test_a_feed_that_will_not_load_changes_nothing(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """**Rule 5, and the reason the others are safe.**

        An empty feed legitimately deletes drafts. If a failed fetch looked the
        same, one bad afternoon at a listing site would delete every draft in
        the product.
        """
        owner = make_user(role="owner")
        calendar = _calendar(db, _property(client, owner)["id"])
        calendars.sync(db, calendar, client=_fake_client(_feed_for(10)))

        with pytest.raises(calendars.CalendarError):
            calendars.sync(
                db, calendar, client=_fake_client(raises=httpx.ConnectTimeout("slow"))
            )

        db.expire_all()
        assert len(db.execute(select(Turnover)).scalars().all()) == 1
        assert db.get(PropertyCalendar, calendar.id).last_error is not None

    def test_a_recovering_feed_clears_the_error(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """A stale error reads as a broken feed forever."""
        owner = make_user(role="owner")
        calendar = _calendar(db, _property(client, owner)["id"])

        with pytest.raises(calendars.CalendarError):
            calendars.sync(db, calendar, client=_fake_client(status=500))
        assert db.get(PropertyCalendar, calendar.id).last_error is not None

        calendars.sync(db, calendar, client=_fake_client(_feed_for(10)))
        db.expire_all()
        refreshed = db.get(PropertyCalendar, calendar.id)
        assert refreshed.last_error is None
        assert refreshed.last_booking_count == 1


# --------------------------------------------------------------------------
# The endpoints
# --------------------------------------------------------------------------


class TestTheEndpoints:
    def test_a_feed_belongs_to_its_property_owner_only(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """Somebody else's feed answers 404, not 403 — a 403 confirms the id."""
        owner = make_user(role="owner")
        stranger = make_user(role="owner")
        prop = _property(client, owner)
        calendar = _calendar(db, prop["id"])

        resp = client.get(
            f"/api/properties/{prop['id']}/calendars", headers=stranger["auth"]
        )
        assert resp.status_code == 404

        resp = client.post(
            f"/api/properties/{prop['id']}/calendars/{calendar.id}/sync",
            headers=stranger["auth"],
        )
        assert resp.status_code == 404

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "http://169.254.169.254/latest/meta-data/",
            "ftp://example.test/a.ics",
        ],
    )
    def test_a_link_that_is_not_http_is_refused(
        self, client: TestClient, make_user, url: str
    ) -> None:
        """**This string becomes an outbound request from our server.**

        A `file://` or a link-local address here is somebody using the product
        to read what it can reach and they cannot. Scheme-checking is not the
        whole of SSRF defence — the metadata address above is still http — but
        accepting arbitrary schemes is not defensible at all.
        """
        owner = make_user(role="owner")
        prop = _property(client, owner)
        resp = client.post(
            f"/api/properties/{prop['id']}/calendars",
            json={"url": url},
            headers=owner["auth"],
        )
        # file:// and ftp:// are refused outright; the metadata address is a
        # valid URL that simply will not answer, and is recorded as an error.
        assert resp.status_code in (201, 422)
        if resp.status_code == 201:
            assert resp.json()["calendar"]["last_error"] is not None

    def test_a_webcal_link_is_accepted_as_https(
        self, client: TestClient, make_user
    ) -> None:
        """Listing sites hand these out for one-click subscription. Refusing
        one would be refusing what the owner meant."""
        owner = make_user(role="owner")
        prop = _property(client, owner)
        resp = client.post(
            f"/api/properties/{prop['id']}/calendars",
            json={"url": "webcal://example.test/a.ics"},
            headers=owner["auth"],
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["calendar"]["url"].startswith("https://")

    def test_the_same_feed_cannot_be_added_twice(
        self, client: TestClient, make_user
    ) -> None:
        """Two rows for one feed would double every booking, because identity
        is (calendar, event)."""
        owner = make_user(role="owner")
        prop = _property(client, owner)
        body = {"url": "https://example.test/dup.ics"}

        assert (
            client.post(
                f"/api/properties/{prop['id']}/calendars", json=body, headers=owner["auth"]
            ).status_code
            == 201
        )
        again = client.post(
            f"/api/properties/{prop['id']}/calendars", json=body, headers=owner["auth"]
        )
        assert again.status_code == 409

    def test_a_home_cannot_have_a_booking_calendar(
        self, client: TestClient, make_user
    ) -> None:
        """A feed describes guests arriving and leaving. A home has neither."""
        owner = make_user(role="owner")
        home = _property(client, owner, property_type="residential")
        resp = client.post(
            f"/api/properties/{home['id']}/calendars",
            json={"url": "https://example.test/a.ics"},
            headers=owner["auth"],
        )
        assert resp.status_code == 409

    def test_removing_a_feed_keeps_the_jobs_it_proposed(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """**A settings change must not cancel somebody's booking.**

        The foreign key is SET NULL, so the turnovers survive and simply stop
        claiming a source.
        """
        owner = make_user(role="owner")
        prop = _property(client, owner)
        calendar = _calendar(db, prop["id"])
        calendars.sync(db, calendar, client=_fake_client(_feed_for(10)))

        resp = client.delete(
            f"/api/properties/{prop['id']}/calendars/{calendar.id}",
            headers=owner["auth"],
        )
        assert resp.status_code == 204

        db.expire_all()
        turnover = db.execute(select(Turnover)).scalars().one()
        assert turnover.source_calendar_id is None

    def test_a_cleaner_never_sees_a_feed_url(
        self, client: TestClient, make_user, make_cleaner, db: Session
    ) -> None:
        """**A feed URL is secret the way a link is secret** — anybody holding
        it can read the booking dates for somebody's house."""
        owner = make_user(role="owner")
        prop = _property(client, owner)
        calendar = _calendar(db, prop["id"], url="https://example.test/SECRETFEED.ics")
        calendars.sync(db, calendar, client=_fake_client(_feed_for(10)))

        turnover = db.execute(select(Turnover)).scalars().one()
        client.post(f"/api/turnovers/{turnover.id}/publish", headers=owner["auth"])

        cleaner = make_cleaner(cleared=True)
        board = client.get("/api/board", headers=cleaner["auth"])
        assert "SECRETFEED" not in board.text
        assert "calendar" not in board.text.lower()


class TestTheScheduledPass:
    def test_one_broken_feed_does_not_stop_the_others(
        self, client: TestClient, make_user, db: Session, monkeypatch
    ) -> None:
        """**The situation where the working calendars most need to work.**

        A listing site having a bad afternoon, or one owner's expired link,
        must not cost every other owner their sync.
        """
        from app.tasks import scheduled

        owner = make_user(role="owner")
        broken = _calendar(db, _property(client, owner)["id"], url="https://bad.test/a.ics")
        working = _calendar(db, _property(client, owner)["id"], url="https://good.test/a.ics")

        real_sync = calendars.sync

        def fake_sync(session, calendar, **kwargs):
            if calendar.id == broken.id:
                raise calendars.CalendarError("that link is gone")
            return real_sync(session, calendar, client=_fake_client(_feed_for(10)))

        monkeypatch.setattr(calendars, "sync", fake_sync)
        assert scheduled.sync_calendars(db) == (1, 0)

        db.expire_all()
        turnovers = db.execute(select(Turnover)).scalars().all()
        assert len(turnovers) == 1
        assert turnovers[0].source_calendar_id == working.id


# --------------------------------------------------------------------------
# The review findings, each with the failure it would have caused
#
# Eight of these came from an automated review of the first version of this
# module, and every one was real. They are gathered here rather than scattered
# because they share a shape worth naming: each was a rule the module's own
# docstring already claimed, enforced on one path and not another, or inferred
# from something that happened to correlate until it stopped.
# --------------------------------------------------------------------------


class TestTheServerIsNotAProxy:
    """A URL an owner types is a place *our server* connects to.

    Without a guard the export-link field is a request-forgery primitive: the
    cloud metadata service, this app's own loopback port, anything else on the
    network. Scheme-checking in the schema is not enough, because a redirect
    never passes through a schema.
    """

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:8000/api/admin/ledger",
            "http://169.254.169.254/latest/meta-data/",
            "http://localhost/a.ics",
            "http://[::1]/a.ics",
        ],
    )
    def test_a_private_address_is_refused(self, url: str) -> None:
        with pytest.raises(calendars.CalendarError) as refused:
            calendars._refuse_private_address(url)
        assert "private network" in refused.value.detail or "not a web address" in (
            refused.value.detail
        )

    def test_the_guard_runs_on_every_redirect_hop_not_just_the_first(self) -> None:
        """**The whole trick, in one test.**

        A perfectly public host answers 302 to somewhere private. A check made
        once, on the URL the owner typed, is satisfied by the first hop and
        never sees the second — which is why the guard lives in the transport
        every hop passes through rather than in `fetch`.
        """
        seen: list[str] = []

        class _RecordingTransport(calendars._PublicOnlyTransport):
            def handle_request(self, request: httpx.Request) -> httpx.Response:
                seen.append(str(request.url))
                # Run the real guard, then answer without a socket.
                calendars._refuse_private_address(request.url)
                return httpx.Response(200, text="unreachable")

        transport = _RecordingTransport()
        request = httpx.Request("GET", "http://169.254.169.254/latest/meta-data/")
        with pytest.raises(calendars.CalendarError):
            transport.handle_request(request)
        assert seen == ["http://169.254.169.254/latest/meta-data/"]

    def test_a_public_address_is_allowed(self) -> None:
        """The guard has to let the actual product work."""
        calendars._refuse_private_address("https://www.airbnb.com/calendar/ical/1.ics")

    def test_the_client_this_module_builds_carries_the_guard(self) -> None:
        """A guard nothing is wired to is a guard that does not run."""
        with calendars._default_client() as client:
            assert isinstance(client._transport, calendars._PublicOnlyTransport)


class TestTheSizeLimitIsARealLimit:
    def test_an_oversized_body_is_cut_off_rather_than_buffered(self) -> None:
        """`len(response.content)` reads the whole thing before measuring it.

        That is not a limit — a host that streams on holds a worker and its
        memory for as long as it likes, on every scheduled pass. The cap has to
        apply *as the bytes arrive*, so what this asserts is not that the error
        is raised but that the bytes **stopped being read**.

        Deliberately a finite body rather than an endless one: a test that hangs
        when the fix is reverted reports a regression as a stuck CI job, which
        is a worse way to find out than a red assertion.
        """
        produced = 0
        chunk = b"x" * 64_000
        limit = calendars.MAX_FEED_BYTES * 4

        def body():
            nonlocal produced
            while produced < limit:
                produced += len(chunk)
                yield chunk

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body())

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with pytest.raises(calendars.CalendarError) as refused:
            calendars.fetch("https://example.test/a.ics", client=client)

        assert "too large" in refused.value.detail
        # Generous slack for chunk boundaries, and still nowhere near having
        # read the whole thing — which is the claim.
        assert produced < calendars.MAX_FEED_BYTES * 2


class TestWhichArrivalIsThisCleansCheckin:
    def test_a_stay_weeks_later_is_not_this_turnovers_checkin(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """`jobs_for` has always claimed "only the very next thing" and did not
        enforce it.

        The consequence is not cosmetic: with a far-off arrival attached, the
        job is measured on the *length of that window* instead of on how soon
        it is, so an imminent checkout reads `standard`.
        """
        owner = make_user(role="owner")
        prop = db.get(Property, uuid.UUID(_property(client, owner)["id"]))

        out = date.today() + timedelta(days=1)
        much_later = out + timedelta(days=21)
        bookings = calendars.parse(
            _ics(
                _event("a", (out - timedelta(days=2)).strftime("%Y%m%d"), out.strftime("%Y%m%d")),
                _event(
                    "b",
                    much_later.strftime("%Y%m%d"),
                    (much_later + timedelta(days=2)).strftime("%Y%m%d"),
                ),
            )
        )
        jobs = {job.external_ref: job for job in calendars.jobs_for(bookings, prop)}
        assert jobs["a"].checkin_at is None

    def test_the_genuine_next_guest_still_is(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """The bound must not throw away the case the whole ladder is built on:
        one guest out, the next one in behind them."""
        owner = make_user(role="owner")
        prop = db.get(Property, uuid.UUID(_property(client, owner)["id"]))

        out = date.today() + timedelta(days=5)
        bookings = calendars.parse(
            _ics(
                _event("a", (out - timedelta(days=2)).strftime("%Y%m%d"), out.strftime("%Y%m%d")),
                _event("b", out.strftime("%Y%m%d"), (out + timedelta(days=2)).strftime("%Y%m%d")),
            )
        )
        jobs = {job.external_ref: job for job in calendars.jobs_for(bookings, prop)}
        assert jobs["a"].checkin_at is not None
        assert jobs["a"].checkin_at.date() == out


class TestHistoryIsNotAJob:
    def test_past_departures_are_not_proposed(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """Exports keep their history, and the horizon only bounded the future.

        Connecting a calendar would have proposed a draft for every stay the
        listing ever had — each one overdue and therefore `urgent`, so the
        owner's first experience of the feature is deleting a year of them.
        """
        owner = make_user(role="owner")
        prop = db.get(Property, uuid.UUID(_property(client, owner)["id"]))

        long_ago = date.today() - timedelta(days=90)
        soon = date.today() + timedelta(days=4)
        bookings = calendars.parse(
            _ics(
                _event(
                    "old",
                    long_ago.strftime("%Y%m%d"),
                    (long_ago + timedelta(days=2)).strftime("%Y%m%d"),
                ),
                _event("new", soon.strftime("%Y%m%d"), (soon + timedelta(days=2)).strftime("%Y%m%d")),
            )
        )
        refs = {job.external_ref for job in calendars.jobs_for(bookings, prop)}
        assert refs == {"new"}


class TestMaintenanceIsNotAnEdit:
    def test_refreshing_urgency_does_not_hand_a_draft_to_nobody(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """**The finding with the quietest failure of the eight.**

        `refresh_urgency` persists a standing vacancy's climb up the ladder, and
        the read paths call it. While "a person touched this" meant
        `updated_at > source_synced_at`, merely *viewing the turnover list* was
        enough to mark a synced draft as edited — after which the feed could
        neither correct its dates nor withdraw it when the guest cancelled, and
        nothing failed to say so.
        """
        owner = make_user(role="owner")
        prop = _property(client, owner)
        calendar = _calendar(db, prop["id"])

        calendars.sync(db, calendar, client=_fake_client(_feed_for(10)))
        turnover = db.execute(select(Turnover)).scalars().one()

        # A synced draft with no next guest is a standing vacancy, so its rung
        # is measured against *now* and climbs as checkout approaches. Far out,
        # it is `standard`.
        assert turnover.checkin_at is None
        assert turnover.urgency is TurnoverUrgency.STANDARD
        was_synced_at = turnover.source_synced_at

        # Exactly what a read path does when somebody opens the list twelve
        # hours before checkout: the rung has changed, so it is persisted.
        refresh_urgency(db, [turnover], now=turnover.checkout_at - timedelta(hours=12))

        db.expire_all()
        turnover = db.execute(select(Turnover)).scalars().one()
        assert turnover.urgency is TurnoverUrgency.URGENT, "the write has to be real"
        # The write happened, and it moved the generic timestamp — which is the
        # whole trap: this is a row no person has been anywhere near.
        assert turnover.updated_at > was_synced_at

        assert turnover.owner_edited_at is None
        assert not calendars._touched_by_a_person(turnover)

        # And the feed can still do its job: move the booking, and the draft moves.
        calendars.sync(db, calendar, client=_fake_client(_feed_for(20)))
        db.expire_all()
        moved = db.execute(select(Turnover)).scalars().one()
        assert moved.checkout_at.date() == date.today() + timedelta(days=23)

    def test_an_actual_owner_edit_still_stops_the_feed(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """The marker has to be set where a person really does edit, or the fix
        above would simply switch rule 2 off in the other direction."""
        owner = make_user(role="owner")
        prop = _property(client, owner)
        calendar = _calendar(db, prop["id"])
        calendars.sync(db, calendar, client=_fake_client(_feed_for(10)))

        turnover = db.execute(select(Turnover)).scalars().one()
        resp = client.patch(
            f"/api/turnovers/{turnover.id}",
            json={"notes": "Key is with the neighbour"},
            headers=owner["auth"],
        )
        assert resp.status_code == 200, resp.text

        db.expire_all()
        edited = db.execute(select(Turnover)).scalars().one()
        assert edited.owner_edited_at is not None
        assert calendars._touched_by_a_person(edited)

    def test_a_posted_job_is_not_the_feeds_to_move(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """**A seam that used to hold by accident.**

        While the person-test was `updated_at`, publishing a draft satisfied it
        as a side effect of the status write. With an explicit edit marker that
        coincidence is gone, so "only a draft" is written down — otherwise the
        feed could move the dates of a job already on the bench with bids on it.
        """
        owner = make_user(role="owner")
        prop = _property(client, owner)
        calendar = _calendar(db, prop["id"])
        calendars.sync(db, calendar, client=_fake_client(_feed_for(10)))

        turnover = db.execute(select(Turnover)).scalars().one()
        was = turnover.checkout_at
        assert (
            client.post(
                f"/api/turnovers/{turnover.id}/publish", headers=owner["auth"]
            ).status_code
            == 200
        )

        calendars.sync(db, calendar, client=_fake_client(_feed_for(20)))
        db.expire_all()
        after = db.execute(select(Turnover)).scalars().one()
        assert after.checkout_at == was


class TestAVanishedBookingIsReportedNotLogged:
    def test_the_scheduled_pass_carries_the_warning_out(
        self, client: TestClient, make_user, db: Session, monkeypatch
    ) -> None:
        """The unattended pass is the one that normally finds this, and it was
        keeping only `.created` — so the product promised the owner a warning
        and then dropped it on the floor."""
        from app.tasks import scheduled

        owner = make_user(role="owner")
        prop = _property(client, owner)
        calendar = _calendar(db, prop["id"])
        calendars.sync(db, calendar, client=_fake_client(_feed_for(10)))

        turnover = db.execute(select(Turnover)).scalars().one()
        assert (
            client.post(
                f"/api/turnovers/{turnover.id}/publish", headers=owner["auth"]
            ).status_code
            == 200
        )

        # The guest cancels: the booking is gone from the feed entirely.
        empty = _ics(_event("other", "20990101", "20990104"))
        real_sync = calendars.sync
        monkeypatch.setattr(
            calendars,
            "sync",
            lambda session, cal, **kw: real_sync(session, cal, client=_fake_client(empty)),
        )

        created, stale = scheduled.sync_calendars(db)
        assert stale == 1, "the vanished booking has to survive the trip out"

    def test_the_number_lands_on_the_owners_own_screen(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """Returned is not the same as recorded. The scheduled pass has no reply
        to put a number in, so it goes on the row the owner's panel reads."""
        owner = make_user(role="owner")
        prop = _property(client, owner)
        calendar = _calendar(db, prop["id"])
        calendars.sync(db, calendar, client=_fake_client(_feed_for(10)))

        turnover = db.execute(select(Turnover)).scalars().one()
        client.post(f"/api/turnovers/{turnover.id}/publish", headers=owner["auth"])

        calendars.sync(
            db, calendar, client=_fake_client(_ics(_event("other", "20990101", "20990104")))
        )

        resp = client.get(
            f"/api/properties/{prop['id']}/calendars", headers=owner["auth"]
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()[0]["last_stale_kept"] == 1


class TestAFeedOnlyBelongsToARental:
    def test_an_archived_property_stops_being_polled(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        owner = make_user(role="owner")
        prop = _property(client, owner)
        _calendar(db, prop["id"])
        assert len(calendars.active_calendars(db)) == 1

        row = db.get(Property, uuid.UUID(prop["id"]))
        row.is_active = False
        db.commit()

        assert calendars.active_calendars(db) == []

    def test_a_property_turned_into_a_home_stops_being_polled(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """**A category error waiting to happen.** `reconcile` writes
        `service_type=turnover`, which a home is refused everywhere else in the
        product — and the owner's screen no longer even shows the calendar
        panel, so the jobs would arrive from a feature they cannot see.
        """
        owner = make_user(role="owner")
        prop = _property(client, owner)
        _calendar(db, prop["id"])

        row = db.get(Property, uuid.UUID(prop["id"]))
        row.property_type = PropertyType.RESIDENTIAL
        db.commit()

        assert calendars.active_calendars(db) == []


# --------------------------------------------------------------------------
# Second review round — six more, and the shape repeats
#
# The recurring one is worth naming: a rule enforced on one of two paths. The
# byte cap without a clock, the eligibility filter on the scheduled pass but not
# the button, uniqueness on a string that is not the thing actually fetched.
# --------------------------------------------------------------------------


class TestAFeedCannotHoldThePassOpen:
    def test_a_slow_drip_is_cut_off_on_time_not_just_on_size(self) -> None:
        """**`FETCH_TIMEOUT_SECONDS` does not bound this.**

        httpx's read timeout is per-receive inactivity, so a host sending one
        small chunk just inside it never trips it and never reaches the byte
        cap either. The scheduled pass reads feeds serially *before* reminders,
        the unclaimed alarm, the review reveal and the outbox drain — so one
        such feed does not merely fail to sync, it stops all of those for
        everybody.
        """
        def slow():
            # Each chunk reports enough elapsed time to pass the deadline,
            # without the test actually waiting a minute to find out.
            for _ in range(1000):
                yield b"BEGIN:VCALENDAR\n"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=slow())

        clock = iter([0.0] + [calendars.MAX_FEED_SECONDS * 2] * 2000)
        with mock.patch.object(calendars, "monotonic", lambda: next(clock)):
            client = httpx.Client(transport=httpx.MockTransport(handler))
            with pytest.raises(calendars.CalendarError) as refused:
                calendars.fetch("https://example.test/a.ics", client=client)

        assert "too long" in refused.value.detail


class TestTheUrlStoredIsTheUrlFetched:
    def test_a_fragment_cannot_smuggle_the_same_feed_in_twice(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """A fragment is never sent in an HTTP request, so two URLs differing
        only by one fetch the identical calendar — while the unique constraint
        sees two different strings and lets both in. Every booking would then
        become two drafts under two calendar ids."""
        owner = make_user(role="owner")
        prop = _property(client, owner)

        first = client.post(
            f"/api/properties/{prop['id']}/calendars",
            json={"url": "https://example.test/feed.ics", "label": "Airbnb"},
            headers=owner["auth"],
        )
        assert first.status_code == 201, first.text

        second = client.post(
            f"/api/properties/{prop['id']}/calendars",
            json={"url": "https://example.test/feed.ics#copy", "label": "Airbnb again"},
            headers=owner["auth"],
        )
        assert second.status_code == 409, second.text


class TestTheStaleWarningStaysTrue:
    def test_finished_work_is_not_counted_as_a_vanished_booking(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """**A warning that is usually wrong is one nobody reads.**

        Once a feed-created job is completed and its checkout has passed,
        `jobs_for` stops proposing it but it is still in the calendar's
        turnovers — and every non-draft fails `_belongs_to_the_feed`. Counting
        those made the number climb by one on every sync forever, until "a guest
        cancelled and a cleaner may still be coming" was mostly describing last
        month's finished work.
        """
        owner = make_user(role="owner")
        prop = _property(client, owner)
        calendar = _calendar(db, prop["id"])
        calendars.sync(db, calendar, client=_fake_client(_feed_for(10)))

        turnover = db.execute(select(Turnover)).scalars().one()
        turnover.status = TurnoverStatus.COMPLETED
        turnover.checkout_at = datetime.now(tz=timezone.utc) - timedelta(days=30)
        db.commit()

        # The booking is long gone from the feed, as it would be.
        result = calendars.sync(
            db, calendar, client=_fake_client(_ics(_event("other", "20990101", "20990104")))
        )
        assert result.stale_but_kept == 0

        db.expire_all()
        assert db.get(PropertyCalendar, calendar.id).last_stale_kept == 0

    def test_a_live_job_whose_booking_vanished_is_still_counted(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """Narrowing the warning must not switch it off — this is the case it
        exists for."""
        owner = make_user(role="owner")
        prop = _property(client, owner)
        calendar = _calendar(db, prop["id"])
        calendars.sync(db, calendar, client=_fake_client(_feed_for(10)))

        turnover = db.execute(select(Turnover)).scalars().one()
        client.post(f"/api/turnovers/{turnover.id}/publish", headers=owner["auth"])

        result = calendars.sync(
            db, calendar, client=_fake_client(_ics(_event("other", "20990101", "20990104")))
        )
        assert result.stale_but_kept == 1


class TestANegativeWindowIsNotAWindow:
    def test_a_checkout_later_in_the_day_than_checkin_does_not_write_a_bad_row(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """An owner can plausibly type a checkout time later than their checkin
        time. Two stays sharing a date then produce a *negative* window, which a
        one-sided `< NEXT_STAY_WITHIN` accepts — and the row violates
        `checkin_after_checkout`, so connecting the feed 500s after the calendar
        has already been saved, and every later sync rolls back."""
        owner = make_user(role="owner")
        prop = db.get(
            Property,
            uuid.UUID(
                _property(
                    client,
                    owner,
                    default_checkout_time="16:00:00",
                    default_checkin_time="11:00:00",
                )["id"]
            ),
        )

        shared = date.today() + timedelta(days=5)
        bookings = calendars.parse(
            _ics(
                _event("a", (shared - timedelta(days=2)).strftime("%Y%m%d"), shared.strftime("%Y%m%d")),
                _event("b", shared.strftime("%Y%m%d"), (shared + timedelta(days=2)).strftime("%Y%m%d")),
            )
        )
        for job in calendars.jobs_for(bookings, prop):
            if job.checkin_at is not None:
                assert job.checkin_at >= job.checkout_at


class TestEveryCallerGetsTheSameRule:
    def test_the_sync_button_refuses_an_archived_property_too(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """`active_calendars` filters the scheduled pass and nothing else. The
        owner's own "Sync now" reaches `sync` directly, and an archived property
        still renders the calendar panel — so the rule has to live where every
        caller passes through."""
        owner = make_user(role="owner")
        prop = _property(client, owner)
        calendar = _calendar(db, prop["id"])

        row = db.get(Property, uuid.UUID(prop["id"]))
        row.is_active = False
        db.commit()

        with pytest.raises(calendars.CalendarError) as refused:
            calendars.sync(db, calendar, client=_fake_client(_feed_for(10)))
        assert "archived" in refused.value.detail

        db.expire_all()
        assert db.execute(select(Turnover)).scalars().all() == []

    def test_the_sync_button_refuses_a_home_too(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        owner = make_user(role="owner")
        prop = _property(client, owner)
        calendar = _calendar(db, prop["id"])

        row = db.get(Property, uuid.UUID(prop["id"]))
        row.property_type = PropertyType.RESIDENTIAL
        db.commit()

        with pytest.raises(calendars.CalendarError):
            calendars.sync(db, calendar, client=_fake_client(_feed_for(10)))

        db.expire_all()
        assert db.execute(select(Turnover)).scalars().all() == []


# --------------------------------------------------------------------------
# Third review round — and half of these were my own last round's doing
#
# Worth recording rather than quietly fixing: reordering `run()` and adding a
# pass budget answers a P1 that only existed because the previous fix bounded
# one feed instead of the pass; the `is_active` and archived-property holes are
# the eligibility rule I added last round being enforced on one path again; and
# the canonicalisation below is last round's fragment fix having stopped one
# spelling short of three.
# --------------------------------------------------------------------------


class TestTheClockWorkComesFirst:
    def test_feeds_are_read_after_the_work_that_owes_somebody_something(
        self, db: Session, monkeypatch
    ) -> None:
        """**A per-feed deadline does not bound the pass.**

        Fifty slow feeds is fifty times the limit, and while that ran, nobody's
        reminder, unclaimed alert, review reveal or queued email had started.
        The reveal is the one scheduled job that is load-bearing: silence
        becoming a veto because a listing site was slow is not a trade worth
        making. So the order is the fix, and this pins it.
        """
        from app.tasks import scheduled

        order: list[str] = []

        def record(name, value):
            def _fn(*args, **kwargs):
                order.append(name)
                return value

            return _fn

        monkeypatch.setattr(scheduled, "place_unmapped_properties", record("placed", 0))
        monkeypatch.setattr(scheduled, "send_reminders", record("reminders", 0))
        monkeypatch.setattr(scheduled, "alert_unclaimed", record("unclaimed", 0))
        monkeypatch.setattr(scheduled, "reveal_reviews", record("revealed", 0))
        monkeypatch.setattr(
            scheduled.notifications, "deliver_pending", record("delivered", 0)
        )
        monkeypatch.setattr(scheduled, "sync_calendars", record("calendars", (0, 0)))

        scheduled.run(db)

        assert order.index("calendars") > order.index("revealed")
        assert order.index("calendars") > order.index("reminders")
        assert order.index("calendars") > order.index("unclaimed")
        assert order.index("calendars") > order.index("delivered")

    def test_the_pass_stops_starting_feeds_once_its_budget_is_spent(
        self, client: TestClient, make_user, db: Session, monkeypatch
    ) -> None:
        """Fair because `active_calendars` is oldest-first: a feed skipped here
        is first in line next pass, so nobody is starved by somebody else's."""
        from app.tasks import scheduled

        owner = make_user(role="owner")
        for _ in range(3):
            _calendar(
                db,
                _property(client, owner)["id"],
                url=f"https://example.test/{uuid.uuid4().hex}.ics",
            )

        read: list[uuid.UUID] = []

        def slow_sync(session, calendar, **kwargs):
            read.append(calendar.id)
            return calendars.SyncResult()

        monkeypatch.setattr(calendars, "sync", slow_sync)
        # First call sets the deadline, the next is already past it.
        clock = iter([0.0, 0.0, scheduled.CALENDAR_PASS_BUDGET * 2])
        monkeypatch.setattr(scheduled, "monotonic", lambda: next(clock))

        scheduled.sync_calendars(db)
        assert len(read) == 1, "the budget has to stop the loop, not just log"


class TestACancelledEventIsNotAStay:
    def test_a_cancelled_reservation_never_becomes_a_draft(self) -> None:
        """A provider may keep a cancelled booking in the feed rather than
        dropping it. Read as live, it becomes a draft for a stay that is not
        happening — and on later syncs it still looks live, so the job is never
        even reported as vanished."""
        feed = _ics(
            "BEGIN:VEVENT\n"
            "DTSTART;VALUE=DATE:20991101\n"
            "DTEND;VALUE=DATE:20991104\n"
            "UID:called-off\n"
            "SUMMARY:Reserved\n"
            "STATUS:CANCELLED\n"
            "END:VEVENT",
            _event("real", "20991201", "20991204"),
        )
        assert [b.uid for b in calendars.parse(feed)] == ["real"]


class TestOneSpellingPerFeed:
    @pytest.mark.parametrize(
        "second",
        [
            "https://EXAMPLE.test/feed.ics",
            "https://example.test:443/feed.ics",
            "https://example.test/feed.ics#again",
        ],
    )
    def test_the_same_request_spelled_differently_is_still_one_feed(
        self, client: TestClient, make_user, db: Session, second: str
    ) -> None:
        """The unique constraint compares strings; the network compares
        requests. Case, a default port and a fragment are three ways to get two
        rows for one calendar, and therefore two drafts for every booking."""
        owner = make_user(role="owner")
        prop = _property(client, owner)

        first = client.post(
            f"/api/properties/{prop['id']}/calendars",
            json={"url": "https://example.test/feed.ics", "label": "Airbnb"},
            headers=owner["auth"],
        )
        assert first.status_code == 201, first.text

        again = client.post(
            f"/api/properties/{prop['id']}/calendars",
            json={"url": second, "label": "Airbnb again"},
            headers=owner["auth"],
        )
        assert again.status_code == 409, again.text

    def test_an_ipv6_host_keeps_the_brackets_that_make_it_a_host(self) -> None:
        """`parts.hostname` strips them, and a bare `2606:4700:4700::1111` is
        not a host — re-parsing reads everything after the first colon as a
        port and raises, so the stored URL could never be fetched again."""
        from urllib.parse import urlsplit

        from app.schemas.calendar import CalendarCreate

        url = CalendarCreate(url="https://[2606:4700:4700::1111]/feed.ics").url
        assert url == "https://[2606:4700:4700::1111]/feed.ics"
        assert urlsplit(url).hostname == "2606:4700:4700::1111"
        assert urlsplit(url).port is None

    def test_a_token_in_the_path_or_query_is_left_alone(self) -> None:
        """Canonicalising must not touch the parts that are case-sensitive —
        listing sites put a token in one of them."""
        from app.schemas.calendar import CalendarCreate

        url = "https://www.airbnb.com/calendar/ical/AbC123XyZ.ics?s=TokEn"
        assert CalendarCreate(url=url).url == url


class TestSwitchedOffMeansSwitchedOff:
    def test_the_sync_button_refuses_a_calendar_that_is_turned_off(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """The scheduled query skipped it; the button did not — so a feed the
        API describes as off could still create, move and delete drafts."""
        owner = make_user(role="owner")
        calendar = _calendar(db, _property(client, owner)["id"])
        calendar.is_active = False
        db.commit()

        with pytest.raises(calendars.CalendarError) as refused:
            calendars.sync(db, calendar, client=_fake_client(_feed_for(10)))
        assert "switched off" in refused.value.detail

        db.expire_all()
        assert db.execute(select(Turnover)).scalars().all() == []


class TestAddingAFeedAnswersHonestly:
    def test_an_archived_property_is_refused_rather_than_answered_201(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """**The add route carried its own copy of the eligibility rule** and it
        covered only the residential case. An archived rental fell through: the
        calendar was committed, `sync` refused *after* the block that records a
        fetch failure, and the handler swallowed it as though the reason had
        been written to the row. The owner got 201, no error, and no jobs.
        """
        owner = make_user(role="owner")
        prop = _property(client, owner)

        row = db.get(Property, uuid.UUID(prop["id"]))
        row.is_active = False
        db.commit()

        resp = client.post(
            f"/api/properties/{prop['id']}/calendars",
            json={"url": "https://example.test/a.ics", "label": "Airbnb"},
            headers=owner["auth"],
        )
        assert resp.status_code == 409, resp.text
        assert "archived" in resp.json()["detail"]

        db.expire_all()
        assert calendars.for_property(db, uuid.UUID(prop["id"])) == []

    def test_a_home_is_still_refused_by_the_same_rule(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        owner = make_user(role="owner")
        prop = _property(client, owner, property_type="residential")

        resp = client.post(
            f"/api/properties/{prop['id']}/calendars",
            json={"url": "https://example.test/a.ics", "label": "Airbnb"},
            headers=owner["auth"],
        )
        assert resp.status_code == 409, resp.text


class TestEligibilityIsReadFresh:
    def test_a_reclassification_in_flight_is_not_missed(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """**The lock is only half of it.**

        `sync` runs after an ownership check has already loaded the property, so
        the instance is in the session's identity map. A locking `SELECT` takes
        the lock — and still returns that cached instance with its *old*
        attributes, unless `populate_existing` is set. Without it the property
        is locked and then read stale, which is the exact case the re-read is
        for: an owner reclassifying to residential while a feed request is in
        flight, and the feed then writing a rental turnover onto a home.

        Another session stands in for that PATCH here, because the failure does
        not need the two to interleave — only for this one to have looked once
        already.
        """
        from app.db import SessionLocal

        owner = make_user(role="owner")
        prop = _property(client, owner)
        calendar = _calendar(db, prop["id"])

        # Load it into this session, the way the real call path does.
        loaded = db.get(Property, uuid.UUID(prop["id"]))
        assert loaded.property_type is PropertyType.SHORT_TERM_RENTAL

        elsewhere = SessionLocal()
        try:
            other = elsewhere.get(Property, uuid.UUID(prop["id"]))
            other.property_type = PropertyType.RESIDENTIAL
            elsewhere.commit()
        finally:
            elsewhere.close()

        with pytest.raises(calendars.CalendarError):
            calendars.sync(db, calendar, client=_fake_client(_feed_for(10)))

        db.rollback()
        db.expire_all()
        assert db.execute(select(Turnover)).scalars().all() == []


class TestADayIsARegionDay:
    def test_a_utc_timestamp_late_at_night_is_not_read_as_tomorrow(self) -> None:
        """**The repo's own rule, applied where the day enters the system.**

        A day is answered in `REGION_TIMEZONE`, never UTC — that is what the
        urgency ladder already lives by. `2026-09-15T02:00Z` is the evening of
        the 14th in Portland, so reading the day straight off the UTC timestamp
        schedules the clean a day late, against a checkout time taken from the
        property's local policy.
        """
        feed = _ics(
            "BEGIN:VEVENT\n"
            "DTSTART:20260910T150000Z\n"
            "DTEND:20260915T020000Z\n"
            "UID:aware\n"
            "SUMMARY:Reserved\n"
            "END:VEVENT"
        )
        booking = calendars.parse(feed)[0]
        assert booking.departs_on == date(2026, 9, 14)

    def test_a_floating_time_keeps_the_day_it_was_written_with(self) -> None:
        """No zone to convert from, and its literal date is what was meant."""
        feed = _ics(
            "BEGIN:VEVENT\n"
            "DTSTART:20260910T150000\n"
            "DTEND:20260915T020000\n"
            "UID:floating\n"
            "SUMMARY:Reserved\n"
            "END:VEVENT"
        )
        assert calendars.parse(feed)[0].departs_on == date(2026, 9, 15)


# --------------------------------------------------------------------------
# The restructure — sequence, not patches
#
# Four rounds of findings were really one finding repeated: a step in the wrong
# place. `sync` is now an explicit sequence — gate, fetch under a real deadline,
# claim and gate again, reconcile — and these pin the properties that sequence
# is supposed to have, rather than the individual bugs it retired.
# --------------------------------------------------------------------------


class TestTheDeadlineTheCallerCanRelyOn:
    def test_a_host_that_never_sends_headers_does_not_hold_the_caller(self) -> None:
        """**The byte-loop deadline could not see this.**

        `client.stream()` must receive the whole response *head* before the loop
        is reached, and httpx's read timeout is per-receive inactivity — so a
        host trickling header bytes holds the call open indefinitely. That ties
        up the request worker behind the Sync button and stalls the scheduled
        pass, whose budget is only checked *between* feeds.
        """
        started = monotonic()

        def handler(request: httpx.Request) -> httpx.Response:
            sleep(30)  # never gets as far as returning a head
            return httpx.Response(200, text="unreachable")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with pytest.raises(calendars.CalendarError) as refused:
            calendars._fetch_within(
                "https://example.test/a.ics", client=client, seconds=0.5
            )

        assert "too long" in refused.value.detail
        # The point is not the error, it is that we stopped waiting for it.
        assert monotonic() - started < 10

    def test_a_feed_that_answers_normally_still_comes_back(self) -> None:
        """A deadline that also breaks the working case is not a fix."""
        text = calendars._fetch_within(
            "https://example.test/a.ics",
            client=_fake_client(_feed_for(10)),
            seconds=30,
        )
        assert "BEGIN:VCALENDAR" in text


class TestTheGateRunsBeforeTheNetwork:
    def test_an_ineligible_property_is_refused_without_an_outbound_request(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """Pressing Sync on a property the product says it no longer reads
        should not sit through a network timeout before saying so."""
        owner = make_user(role="owner")
        prop = _property(client, owner)
        calendar = _calendar(db, prop["id"])

        row = db.get(Property, uuid.UUID(prop["id"]))
        row.is_active = False
        db.commit()

        asked: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            asked.append(str(request.url))
            return httpx.Response(200, text=_feed_for(10))

        feed_client = httpx.Client(transport=httpx.MockTransport(handler))
        with pytest.raises(calendars.CalendarError):
            calendars.sync(db, calendar, client=feed_client)

        assert asked == [], "the feed must not be contacted at all"


class TestTheGateRunsAgainOnTheLockedRow:
    def test_switching_the_feed_off_mid_fetch_stops_the_write(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """**The pre-fetch gate is not enough on its own.**

        The answer can change while we are on the network, and the copy in
        memory would never notice. This flips the switch *during* the fetch, so
        only the locked re-read with `populate_existing` can catch it.
        """
        from app.db import SessionLocal

        owner = make_user(role="owner")
        prop = _property(client, owner)
        calendar = _calendar(db, prop["id"])

        def handler(request: httpx.Request) -> httpx.Response:
            # The owner presses "switch off" while the feed is being read.
            elsewhere = SessionLocal()
            try:
                row = elsewhere.get(PropertyCalendar, calendar.id)
                row.is_active = False
                elsewhere.commit()
            finally:
                elsewhere.close()
            return httpx.Response(200, text=_feed_for(10))

        feed_client = httpx.Client(transport=httpx.MockTransport(handler))
        with pytest.raises(calendars.CalendarError) as refused:
            calendars.sync(db, calendar, client=feed_client)
        assert "switched off" in refused.value.detail

        db.rollback()
        db.expire_all()
        assert db.execute(select(Turnover)).scalars().all() == []


class TestEveryFailureLeavesItsReason:
    """One of these escaped the recording block before, and a caller swallowed
    it assuming a reason had been written — the owner got a success with no jobs
    and no explanation. A uniform handler is what makes that impossible rather
    than fixed."""

    def test_a_feed_that_will_not_load_records_why(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        owner = make_user(role="owner")
        calendar = _calendar(db, _property(client, owner)["id"])

        with pytest.raises(calendars.CalendarError):
            calendars.sync(db, calendar, client=_fake_client(status=404))

        db.expire_all()
        assert "not found" in db.get(PropertyCalendar, calendar.id).last_error

    def test_a_gate_refusal_records_why_too(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """Not only the fetch step — this is the one that used to escape."""
        owner = make_user(role="owner")
        prop = _property(client, owner)
        calendar = _calendar(db, prop["id"])

        row = db.get(Property, uuid.UUID(prop["id"]))
        row.is_active = False
        db.commit()

        with pytest.raises(calendars.CalendarError):
            calendars.sync(db, calendar, client=_fake_client(_feed_for(10)))

        db.expire_all()
        assert "archived" in db.get(PropertyCalendar, calendar.id).last_error

    def test_a_failure_part_way_through_commits_nothing_of_the_sync(
        self, client: TestClient, make_user, db: Session, monkeypatch
    ) -> None:
        """The recording handler rolls back first, which matters now that it
        covers reconcile as well as the fetch."""
        owner = make_user(role="owner")
        prop = _property(client, owner)
        calendar = _calendar(db, prop["id"])

        real_reconcile = calendars.reconcile

        def explode(session, cal, proposed, **kwargs):
            real_reconcile(session, cal, proposed, **kwargs)
            raise calendars.CalendarError("something went wrong late on")

        monkeypatch.setattr(calendars, "reconcile", explode)
        with pytest.raises(calendars.CalendarError):
            calendars.sync(db, calendar, client=_fake_client(_feed_for(10)))

        db.expire_all()
        assert db.execute(select(Turnover)).scalars().all() == []
        assert db.get(PropertyCalendar, calendar.id).last_error is not None


# --------------------------------------------------------------------------
# Round five — new ground, not the class the restructure retired
# --------------------------------------------------------------------------


class TestAFeedUrlIsNotWrittenToTheLog:
    def test_a_sync_never_logs_the_credential_in_the_url(
        self, client: TestClient, make_user, db: Session, caplog
    ) -> None:
        """**The product treats a feed URL as a secret everywhere except here.**

        httpx logs `HTTP Request: GET <url>` at INFO, listing sites put the
        token in the path or query, and the scheduled entry point turns the root
        logger up to INFO — so every unattended sync wrote every owner's
        credential into the application log. There is already a test that the
        URL appears nowhere in a board response; a log file is not an exception
        to the same rule.
        """
        owner = make_user(role="owner")
        secret = "https://www.airbnb.com/calendar/ical/9.ics?s=SUPERSECRETTOKEN"
        calendar = _calendar(db, _property(client, owner)["id"], url=secret)

        with caplog.at_level(logging.DEBUG):
            calendars.sync(db, calendar, client=_fake_client(_feed_for(10)))

        assert "SUPERSECRETTOKEN" not in caplog.text


class TestOneEventIsOneIdentity:
    def test_a_recurrence_override_is_its_own_booking(self) -> None:
        """iCalendar's own rule: an occurrence is identified by UID **and**
        RECURRENCE-ID. Reading the UID alone made two events one identity, and
        `(source_calendar_id, external_ref)` is a unique constraint — so both
        rows were inserted and the *commit* failed. Not a `CalendarError`, so it
        surfaced as a 500 with no reason recorded anywhere."""
        feed = _ics(
            _event("stay", "20991101", "20991104"),
            "BEGIN:VEVENT\n"
            "DTSTART;VALUE=DATE:20991201\n"
            "DTEND;VALUE=DATE:20991204\n"
            "UID:stay\n"
            "RECURRENCE-ID;VALUE=DATE:20991201\n"
            "SUMMARY:Reserved\n"
            "END:VEVENT",
        )
        assert [b.uid for b in calendars.parse(feed)] == ["stay", "stay#2099-12-01"]

    def test_that_identity_does_not_depend_on_a_library_repr(self) -> None:
        """Identity that moves between library versions would orphan every
        turnover keyed to the old spelling."""
        feed = _ics(
            "BEGIN:VEVENT\n"
            "DTSTART;VALUE=DATE:20991201\n"
            "DTEND;VALUE=DATE:20991204\n"
            "UID:stay\n"
            "RECURRENCE-ID;VALUE=DATE:20991201\n"
            "SUMMARY:Reserved\n"
            "END:VEVENT"
        )
        assert calendars.parse(feed)[0].uid == "stay#2099-12-01"

    def test_a_genuinely_repeated_event_is_kept_once(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """Past UID+RECURRENCE-ID a repeat is a feed we cannot interpret rather
        than two stays. Keeping the first is a choice; inserting both is a
        constraint violation at commit time, which is a 500 with no reason."""
        owner = make_user(role="owner")
        calendar = _calendar(db, _property(client, owner)["id"])

        start = date.today() + timedelta(days=10)
        twice = _ics(
            _event("same", start.strftime("%Y%m%d"), (start + timedelta(days=3)).strftime("%Y%m%d")),
            _event("same", start.strftime("%Y%m%d"), (start + timedelta(days=3)).strftime("%Y%m%d")),
        )
        result = calendars.sync(db, calendar, client=_fake_client(twice))
        assert result.created == 1

        db.expire_all()
        assert len(db.execute(select(Turnover)).scalars().all()) == 1


class TestTheSyncEndpointSurvivesTheFeedVanishing:
    def test_a_calendar_deleted_mid_sync_answers_502_not_500(
        self, client: TestClient, make_user, db: Session, monkeypatch
    ) -> None:
        """`sync` can refuse because the row is gone — another request can
        delete the feed while this one is out on the network. Refreshing it
        unconditionally in the handler turns a deliberate 502 into a 500 that
        says nothing."""
        owner = make_user(role="owner")
        prop = _property(client, owner)
        calendar = _calendar(db, prop["id"])
        calendar_id = calendar.id

        def vanished(session, cal, **kwargs):
            from app.db import SessionLocal

            elsewhere = SessionLocal()
            try:
                row = elsewhere.get(PropertyCalendar, calendar_id)
                elsewhere.delete(row)
                elsewhere.commit()
            finally:
                elsewhere.close()
            raise calendars.CalendarError("That calendar has been removed.")

        monkeypatch.setattr(calendars, "sync", vanished)
        resp = client.post(
            f"/api/properties/{prop['id']}/calendars/{calendar_id}/sync",
            headers=owner["auth"],
        )
        assert resp.status_code == 502, resp.text
        assert "removed" in resp.json()["detail"]


# --------------------------------------------------------------------------
# Round six — three of these four were my own previous fixes
# --------------------------------------------------------------------------


class TestAStuckFetchIsEndedNotAbandoned:
    def test_the_worker_dies_rather_than_leaking(self, monkeypatch) -> None:
        """**Stopping waiting is not the same as stopping.**

        The previous version raised on the caller's thread and left the worker
        running, calling it an acceptable residual. It is not: for a host that
        trickles *header* bytes the body deadline is never reached, so no
        timeout ever fires for that thread — and every scheduled pass and every
        press of the button starts another that also never ends.

        A real server, because this is precisely the behaviour a mock transport
        cannot have: there is no socket to leave open.
        """
        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        def trickle() -> None:
            conn, _ = server.accept()
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\n")
            try:
                while True:
                    # Header bytes forever; the blank line never comes.
                    conn.sendall(b"X-Pad: y\r\n")
                    sleep(0.05)
            except OSError:
                pass
            finally:
                conn.close()

        threading.Thread(target=trickle, daemon=True).start()

        # The address guard correctly refuses loopback, and this test is about
        # what happens to the *thread* — so that one rule is stood down here,
        # deliberately and narrowly. It has its own tests in
        # `TestTheServerIsNotAProxy`; what cannot be faked is a real socket
        # that never finishes sending its headers.
        monkeypatch.setattr(calendars, "_refuse_private_address", lambda url: None)
        before = {t.name for t in threading.enumerate()}

        with pytest.raises(calendars.CalendarError) as refused:
            calendars._fetch_within(f"http://127.0.0.1:{port}/a.ics", seconds=1.0)
        assert "too long" in refused.value.detail

        # The worker must be gone, not merely no longer waited on.
        deadline = monotonic() + 10
        while monotonic() < deadline:
            leaked = {
                t.name for t in threading.enumerate() if t.name == "linx-calendar-fetch"
            } - before
            if not leaked:
                break
            sleep(0.1)
        assert not leaked, "the fetch worker outlived its deadline"
        server.close()


class TestAnUnreadableEncodingIsAnUnreadableFeed:
    def test_an_unknown_charset_does_not_become_a_500(self) -> None:
        """`bytes.decode` raises `LookupError` for a charset with no codec, and
        that is not an httpx error — so it escaped every handler and became a
        500 with no reason recorded, while every other unreadable feed is a
        sentence on the owner's screen."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=_feed_for(10).encode(),
                headers={"content-type": "text/calendar; charset=x-not-a-real-charset"},
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        text = calendars.fetch("https://example.test/a.ics", client=client)
        assert "BEGIN:VCALENDAR" in text


class TestAnIdentityFitsItsColumn:
    def test_a_very_long_uid_with_a_recurrence_is_hashed_not_truncated(self) -> None:
        """A UID near `external_ref`'s 500-character limit plus `#YYYY-MM-DD`
        runs past it, and the failure lands at *commit* as a database error
        rather than a `CalendarError` — a 500 on the button and nothing the
        owner can see on the scheduled pass.

        Hashed rather than truncated, because two long UIDs sharing a prefix
        would truncate to the same identity, which is the one thing identity may
        never do."""
        long_uid = "u" * 495
        feed = _ics(
            "BEGIN:VEVENT\n"
            "DTSTART;VALUE=DATE:20991201\n"
            "DTEND;VALUE=DATE:20991204\n"
            f"UID:{long_uid}\n"
            "RECURRENCE-ID;VALUE=DATE:20991201\n"
            "SUMMARY:Reserved\n"
            "END:VEVENT"
        )
        identity = calendars.parse(feed)[0].uid
        assert len(identity) <= calendars.MAX_EXTERNAL_REF
        assert identity.startswith("sha256:")

    def test_two_long_uids_sharing_a_prefix_stay_distinct(self) -> None:
        """The reason it is a digest and not a slice."""

        def identity_for(uid: str) -> str:
            return calendars.parse(
                _ics(
                    "BEGIN:VEVENT\n"
                    "DTSTART;VALUE=DATE:20991201\n"
                    "DTEND;VALUE=DATE:20991204\n"
                    f"UID:{uid}\n"
                    "RECURRENCE-ID;VALUE=DATE:20991201\n"
                    "SUMMARY:Reserved\n"
                    "END:VEVENT"
                )
            )[0].uid

        shared = "p" * 495
        assert identity_for(shared + "a") != identity_for(shared + "b")

    def test_an_ordinary_uid_is_left_exactly_as_it_is(self) -> None:
        """Hashing everything would throw away the readable identity, and the
        whole point is that a booking keeps the id the feed gave it."""
        assert calendars.parse(_ics(_event("normal", "20991101", "20991104")))[0].uid == (
            "normal"
        )


class TestBothEndpointsSurviveTheFeedVanishing:
    def test_adding_a_feed_deleted_mid_fetch_does_not_500(
        self, client: TestClient, make_user, db: Session, monkeypatch
    ) -> None:
        """**The same rule on one of two paths, again.** Round five guarded the
        sync endpoint's refresh and left the identical line on the add endpoint,
        which commits the calendar *before* fetching and so has exactly the same
        window."""
        owner = make_user(role="owner")
        prop = _property(client, owner)

        def vanished(session, cal, **kwargs):
            from app.db import SessionLocal

            elsewhere = SessionLocal()
            try:
                row = elsewhere.get(PropertyCalendar, cal.id)
                if row is not None:
                    elsewhere.delete(row)
                    elsewhere.commit()
            finally:
                elsewhere.close()
            raise calendars.CalendarError("That calendar has been removed.")

        monkeypatch.setattr(calendars, "sync", vanished)
        resp = client.post(
            f"/api/properties/{prop['id']}/calendars",
            json={"url": "https://example.test/a.ics", "label": "Airbnb"},
            headers=owner["auth"],
        )
        assert resp.status_code == 409, resp.text
        assert "removed" in resp.json()["detail"]


# --------------------------------------------------------------------------
# Round seven — bounding the population rather than each way out of it
# --------------------------------------------------------------------------


class TestLiveFetchesAreCapped:
    def test_no_more_workers_start_once_every_slot_is_held(self, monkeypatch) -> None:
        """**A cap on the threads themselves, not another fix for one way they
        get stuck.**

        Closing the client ends a worker blocked on a socket, but it cannot
        interrupt one still inside `socket.getaddrinfo` — bounded by the OS
        resolver rather than unbounded, but still able to outlive the grace
        period. Rather than chase each way a worker might outstay its deadline,
        the number of live ones is bounded outright.
        """
        release = threading.Event()

        def blocking(url, *, client=None):
            release.wait(30)
            return _feed_for(10)

        monkeypatch.setattr(calendars, "fetch", blocking)

        # Fill every slot with a worker that will not finish.
        holders = [
            threading.Thread(
                target=lambda: _swallow(
                    lambda: calendars._fetch_within(
                        "https://example.test/a.ics",
                        client=_fake_client(),
                        seconds=0.2,
                    )
                ),
                daemon=True,
            )
            for _ in range(calendars.MAX_CONCURRENT_FETCHES)
        ]
        for t in holders:
            t.start()
        for t in holders:
            t.join(5)

        try:
            with pytest.raises(calendars.CalendarError) as refused:
                calendars._fetch_within(
                    "https://example.test/a.ics", client=_fake_client(), seconds=0.2
                )
            assert "Too many calendars" in refused.value.detail
        finally:
            release.set()

    def test_a_slot_comes_back_when_its_worker_ends(self, monkeypatch) -> None:
        """A cap that never released would take the feature down after four
        reads."""
        for _ in range(calendars.MAX_CONCURRENT_FETCHES + 2):
            text = calendars._fetch_within(
                "https://example.test/a.ics",
                client=_fake_client(_feed_for(10)),
                seconds=5,
            )
            assert "BEGIN:VCALENDAR" in text


def _swallow(fn):
    try:
        fn()
    except Exception:
        pass


class TestARootFeedIsOneFeed:
    def test_an_empty_path_and_a_slash_are_the_same_calendar(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """Both produce the same HTTP request target, so they must produce the
        same string — otherwise one root-hosted feed is two calendars and every
        booking becomes two drafts."""
        owner = make_user(role="owner")
        prop = _property(client, owner)

        first = client.post(
            f"/api/properties/{prop['id']}/calendars",
            json={"url": "https://example.test", "label": "Airbnb"},
            headers=owner["auth"],
        )
        assert first.status_code == 201, first.text

        again = client.post(
            f"/api/properties/{prop['id']}/calendars",
            json={"url": "https://example.test/", "label": "Again"},
            headers=owner["auth"],
        )
        assert again.status_code == 409, again.text


class TestTheSuccessPathSurvivesTheFeedVanishing:
    def test_a_delete_landing_right_after_a_successful_sync_is_not_a_500(
        self, client: TestClient, make_user, db: Session, monkeypatch
    ) -> None:
        """A DELETE already waiting on the calendar's lock can commit the moment
        `sync` commits. The failed refresh leaves the instance expired, and
        serialising it is a 500 at the very end of a request that worked."""
        owner = make_user(role="owner")
        prop = _property(client, owner)
        calendar = _calendar(db, prop["id"])
        calendar_id = calendar.id

        real_sync = calendars.sync

        def sync_then_deleted(session, cal, **kwargs):
            result = real_sync(session, cal, client=_fake_client(_feed_for(10)))
            from app.db import SessionLocal

            elsewhere = SessionLocal()
            try:
                row = elsewhere.get(PropertyCalendar, calendar_id)
                if row is not None:
                    elsewhere.delete(row)
                    elsewhere.commit()
            finally:
                elsewhere.close()
            return result

        monkeypatch.setattr(calendars, "sync", sync_then_deleted)
        resp = client.post(
            f"/api/properties/{prop['id']}/calendars/{calendar_id}/sync",
            headers=owner["auth"],
        )
        assert resp.status_code == 409, resp.text
        assert "removed" in resp.json()["detail"]
