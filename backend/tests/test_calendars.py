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

import uuid
from datetime import date, datetime, time, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.calendar import PropertyCalendar
from app.models.enums import TurnoverStatus
from app.models.property import Property
from app.models.turnover import Turnover
from app.services import calendars

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
        assert scheduled.sync_calendars(db) == 1

        db.expire_all()
        turnovers = db.execute(select(Turnover)).scalars().all()
        assert len(turnovers) == 1
        assert turnovers[0].source_calendar_id == working.id
