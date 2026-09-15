"""Several jobs at once, for the owner a calendar feed cannot serve.

A booking feed is the fast path onto the board, and two kinds of owner cannot
use it at all. A **home has no booking calendar** — that is what a home is —
and a rental booked direct or by phone has **no `.ics` URL** to paste. Both
were left typing one job per screen, which is fine for one job and absurd for
a season.

The rules below all come from one decision: this is the owner speaking, not a
feed. So it writes drafts rather than posting, it refuses the whole list rather
than half of it, and it is not reconciled against anything afterwards — the
rows are ordinary turnovers the owner owns from the moment they exist.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.turnover import Turnover
from app.services import calendars as calendars_module
from app.services import turnovers as turnover_rules


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


def _days(*offsets: int) -> list[dict]:
    base = datetime.now(timezone.utc)
    return [{"checkout_at": (base + timedelta(days=d)).isoformat()} for d in offsets]


def _bulk(client: TestClient, owner: dict, property_id: str, **overrides):
    payload = {"property_id": property_id, "jobs": _days(3, 10, 17), **overrides}
    return client.post("/api/turnovers/bulk", json=payload, headers=owner["auth"])


class TestTheOwnerAFeedCannotServe:
    def test_a_home_can_have_a_season_of_cleans_added_at_once(
        self, client: TestClient, make_user
    ) -> None:
        """The case that has no alternative at all: a home never has a feed."""
        owner = make_user(role="owner")
        home = _property(client, owner, property_type="residential")

        resp = _bulk(client, owner, home["id"], service_type="deep")
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert len(body["created"]) == 3
        assert {job["service_type"] for job in body["created"]} == {"deep"}
        assert all(job["checkin_at"] is None for job in body["created"])

    def test_a_rental_without_a_feed_can_too(
        self, client: TestClient, make_user
    ) -> None:
        owner = make_user(role="owner")
        rental = _property(client, owner)
        resp = _bulk(client, owner, rental["id"])
        assert resp.status_code == 201, resp.text
        assert len(resp.json()["created"]) == 3

    def test_somebody_elses_property_is_not_found_rather_than_forbidden(
        self, client: TestClient, make_user
    ) -> None:
        """A 403 confirms the id exists — the house rule everywhere here."""
        owner = make_user(role="owner")
        stranger = make_user(role="owner")
        prop = _property(client, owner)
        resp = _bulk(client, stranger, prop["id"])
        assert resp.status_code == 404

    def test_a_cleaner_cannot_post_jobs(
        self, client: TestClient, make_cleaner, make_user
    ) -> None:
        owner = make_user(role="owner")
        prop = _property(client, owner)
        cleaner = make_cleaner(cleared=True)
        resp = _bulk(client, cleaner, prop["id"])
        assert resp.status_code == 403


class TestDraftsNeverPosted:
    """**Calendars rule 1, in its other spelling.**

    An owner who wanted ten jobs on the bench can post them in a minute; an
    owner who did not cannot unsend the alerts, the bids, or the apology. A
    bulk form is precisely where a wrong paste becomes twenty jobs, so the one
    irreversible step is the one it does not take.
    """

    def test_everything_lands_as_a_draft(
        self, client: TestClient, make_user
    ) -> None:
        owner = make_user(role="owner")
        prop = _property(client, owner)
        body = _bulk(client, owner, prop["id"]).json()
        assert {job["status"] for job in body["created"]} == {"draft"}

    def test_there_is_no_way_to_ask_for_them_posted(
        self, client: TestClient, make_user
    ) -> None:
        """Not a default somebody can flip — there is no flag at all. A
        `publish` that silently did nothing would be worse than its absence."""
        owner = make_user(role="owner")
        prop = _property(client, owner)
        resp = client.post(
            "/api/turnovers/bulk",
            json={
                "property_id": prop["id"],
                "jobs": _days(4),
                "publish": True,
            },
            headers=owner["auth"],
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["created"][0]["status"] == "draft"

    def test_nobody_is_notified(
        self, client: TestClient, make_user, make_cleaner, db: Session
    ) -> None:
        """A draft is nobody's business but the owner's, which is why this
        added no `NotificationEvent` — the closed list stays closed."""
        from app.models.notification import Notification

        make_cleaner(cleared=True)
        owner = make_user(role="owner")
        prop = _property(client, owner)
        before = db.execute(select(Notification)).scalars().all()
        _bulk(client, owner, prop["id"])
        after = db.execute(select(Notification)).scalars().all()
        assert len(after) == len(before), "a draft told somebody"


class TestRefusedNeverCorrected:
    def test_a_checkin_on_a_home_refuses_the_whole_list(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """A checkin on a home is a category error, not a stray value — and on
        a list it refuses all of it, naming the row."""
        owner = make_user(role="owner")
        home = _property(client, owner, property_type="residential")
        base = datetime.now(timezone.utc)
        jobs = _days(3, 10)
        jobs.append(
            {
                "checkout_at": (base + timedelta(days=17)).isoformat(),
                "checkin_at": (base + timedelta(days=18)).isoformat(),
            }
        )

        resp = client.post(
            "/api/turnovers/bulk",
            json={"property_id": home["id"], "jobs": jobs},
            headers=owner["auth"],
        )
        assert resp.status_code == 409, resp.text
        assert "Row 3" in resp.json()["detail"]

        landed = db.execute(
            select(Turnover).where(Turnover.property_id == uuid.UUID(home["id"]))
        ).scalars().all()
        assert landed == [], (
            "two rows were written before the third was refused, so the owner "
            "cannot retry without creating duplicates"
        )

    def test_a_scope_the_property_does_not_take_refuses(
        self, client: TestClient, make_user
    ) -> None:
        owner = make_user(role="owner")
        home = _property(client, owner, property_type="residential")
        resp = _bulk(client, owner, home["id"], service_type="turnover")
        assert resp.status_code == 409
        assert "not turnover" in resp.json()["detail"]

    def test_an_archived_property_refuses(
        self, client: TestClient, make_user
    ) -> None:
        owner = make_user(role="owner")
        prop = _property(client, owner)
        client.delete(f"/api/properties/{prop['id']}", headers=owner["auth"])
        resp = _bulk(client, owner, prop["id"])
        assert resp.status_code == 409

    def test_a_naive_timestamp_refuses(
        self, client: TestClient, make_user
    ) -> None:
        """One stray naive value among forty correct ones is exactly what a
        paste produces, and reading it as UTC moves a Maine checkout by hours."""
        owner = make_user(role="owner")
        prop = _property(client, owner)
        resp = client.post(
            "/api/turnovers/bulk",
            json={
                "property_id": prop["id"],
                "jobs": [{"checkout_at": "2027-03-04T11:00:00"}],
            },
            headers=owner["auth"],
        )
        assert resp.status_code == 422


class TestPastingTwice:
    """A duplicate is not a mismatch to refuse — the owner asked for a job that
    is already there — but dropping it silently is how somebody pastes twice
    and never learns the second one did nothing."""

    def test_a_date_already_on_the_board_is_reported_not_repeated(
        self, client: TestClient, make_user
    ) -> None:
        owner = make_user(role="owner")
        prop = _property(client, owner)
        # The same list twice, which is what a second paste actually is —
        # `_days()` would re-read the clock and produce different instants.
        jobs = _days(3, 10, 17)
        payload = {"property_id": prop["id"], "jobs": jobs}

        first = client.post(
            "/api/turnovers/bulk", json=payload, headers=owner["auth"]
        ).json()
        assert len(first["created"]) == 3
        assert first["already_there"] == []

        again = client.post(
            "/api/turnovers/bulk", json=payload, headers=owner["auth"]
        ).json()
        assert again["created"] == []
        assert len(again["already_there"]) == 3

    def test_a_date_repeated_inside_one_submission_lands_once(
        self, client: TestClient, make_user
    ) -> None:
        owner = make_user(role="owner")
        prop = _property(client, owner)
        when = (datetime.now(timezone.utc) + timedelta(days=6)).isoformat()
        resp = client.post(
            "/api/turnovers/bulk",
            json={
                "property_id": prop["id"],
                "jobs": [{"checkout_at": when}, {"checkout_at": when}],
            },
            headers=owner["auth"],
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert len(body["created"]) == 1
        assert len(body["already_there"]) == 1

    def test_a_cancelled_job_does_not_block_re_entering_its_date(
        self, client: TestClient, make_user
    ) -> None:
        """An owner who called a job off and is re-entering that date means it;
        refusing would be the system arguing about their own calendar."""
        owner = make_user(role="owner")
        prop = _property(client, owner)
        when = (datetime.now(timezone.utc) + timedelta(days=9)).isoformat()
        created = client.post(
            "/api/turnovers/bulk",
            json={"property_id": prop["id"], "jobs": [{"checkout_at": when}]},
            headers=owner["auth"],
        ).json()["created"][0]
        cancelled = client.post(
            f"/api/turnovers/{created['id']}/cancel",
            json={"reason": "Guest called it off."},
            headers=owner["auth"],
        )
        assert cancelled.status_code == 200, cancelled.text

        again = client.post(
            "/api/turnovers/bulk",
            json={"property_id": prop["id"], "jobs": [{"checkout_at": when}]},
            headers=owner["auth"],
        )
        assert again.status_code == 201, again.text
        assert len(again.json()["created"]) == 1


class TestAMistakeStaysSmall:
    def test_more_than_the_cap_refuses(
        self, client: TestClient, make_user
    ) -> None:
        """The failure a cap prevents is not a big request — it is an owner who
        meant six jobs and posted six hundred."""
        owner = make_user(role="owner")
        prop = _property(client, owner)
        resp = client.post(
            "/api/turnovers/bulk",
            json={
                "property_id": prop["id"],
                "jobs": _days(*range(1, turnover_rules.MAX_BULK_JOBS + 2)),
            },
            headers=owner["auth"],
        )
        assert resp.status_code == 409
        assert str(turnover_rules.MAX_BULK_JOBS) in resp.json()["detail"]

    def test_an_empty_list_refuses(self, client: TestClient, make_user) -> None:
        owner = make_user(role="owner")
        prop = _property(client, owner)
        resp = client.post(
            "/api/turnovers/bulk",
            json={"property_id": prop["id"], "jobs": []},
            headers=owner["auth"],
        )
        assert resp.status_code == 422


class TestUrgencyIsStillDerivedHere:
    """`urgency` has exactly one author, and a new write path is exactly where
    a second one appears — a column defaulted to `standard` rather than
    measured would make the bench board sort a season of work wrongly and
    nothing would fail."""

    @pytest.mark.parametrize(
        ("hours_out", "expected"),
        [(6, "urgent"), (40, "soon"), (24 * 9, "standard")],
    )
    def test_each_row_is_measured_not_defaulted(
        self, client: TestClient, make_user, hours_out: int, expected: str
    ) -> None:
        owner = make_user(role="owner")
        home = _property(client, owner, property_type="residential")
        when = datetime.now(timezone.utc) + timedelta(hours=hours_out)
        resp = client.post(
            "/api/turnovers/bulk",
            json={
                "property_id": home["id"],
                "jobs": [{"checkout_at": when.isoformat()}],
            },
            headers=owner["auth"],
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["created"][0]["urgency"] == expected

    def test_same_day_stays_unreachable_on_a_home(
        self, client: TestClient, make_user
    ) -> None:
        """It describes a guest arriving the day another leaves, which a home
        does not have — so no bulk row on one may ever land on that rung."""
        owner = make_user(role="owner")
        home = _property(client, owner, property_type="residential")
        body = _bulk(client, owner, home["id"]).json()
        assert all(job["urgency"] != "same_day" for job in body["created"])
        assert all(job["is_same_day"] is False for job in body["created"])


# --------------------------------------------------------------------------
# Reading an uploaded `.ics`
#
# For the owner whose listing site will export a file but will not hand over a
# sync URL. It writes nothing: no calendar row, no `external_ref`, nothing
# reconciled afterwards. It fills in the bulk form and then it is over.
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


def _stay(days_out: int, uid: str, nights: int = 3) -> str:
    from datetime import date

    start = date.today() + timedelta(days=days_out)
    return _event(
        uid,
        start.strftime("%Y%m%d"),
        (start + timedelta(days=nights)).strftime("%Y%m%d"),
    )


def _upload(client: TestClient, owner: dict, property_id: str, text: str, **kw):
    return client.post(
        f"/api/properties/{property_id}/calendars/read-file",
        files={"file": ("bookings.ics", text.encode(), "text/calendar")},
        headers=owner["auth"],
        **kw,
    )


class TestAnUploadedCalendar:
    def test_it_proposes_the_same_jobs_a_feed_would(
        self, client: TestClient, make_user
    ) -> None:
        """The same two steps as the sync path — `parse` then `jobs_for` — so a
        file and a URL cannot disagree about what a booking implies."""
        owner = make_user(role="owner")
        rental = _property(client, owner)
        resp = _upload(
            client, owner, rental["id"], _ics(_stay(5, "a"), _stay(12, "b"))
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["bookings_seen"] == 2
        assert len(body["jobs"]) == 2
        assert all(job["checkout_at"] for job in body["jobs"])

    def test_it_writes_nothing(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """**Not a second kind of feed.** No calendar row, no turnover, no
        identity to reconcile later."""
        from app.models.calendar import PropertyCalendar

        owner = make_user(role="owner")
        rental = _property(client, owner)
        _upload(client, owner, rental["id"], _ics(_stay(5, "a")))

        prop_id = uuid.UUID(rental["id"])
        assert (
            db.execute(
                select(Turnover).where(Turnover.property_id == prop_id)
            ).scalars().all()
            == []
        )
        assert (
            db.execute(
                select(PropertyCalendar).where(
                    PropertyCalendar.property_id == prop_id
                )
            ).scalars().all()
            == []
        )

    def test_a_home_refuses_it_in_the_feed_paths_own_words(
        self, client: TestClient, make_user
    ) -> None:
        """`calendars.refuse_ineligible` is the one author of "does a booking
        calendar make sense here", asked rather than copied — so a home gets
        the same refusal whether the calendar arrives as a URL or a file."""
        owner = make_user(role="owner")
        home = _property(client, owner, property_type="residential")
        resp = _upload(client, owner, home["id"], _ics(_stay(5, "a")))
        assert resp.status_code == 409
        assert "a home does not have" in resp.json()["detail"].lower()

    def test_an_archived_property_refuses(
        self, client: TestClient, make_user
    ) -> None:
        owner = make_user(role="owner")
        rental = _property(client, owner)
        client.delete(f"/api/properties/{rental['id']}", headers=owner["auth"])
        resp = _upload(client, owner, rental["id"], _ics(_stay(5, "a")))
        assert resp.status_code == 409

    def test_somebody_elses_property_is_not_found(
        self, client: TestClient, make_user
    ) -> None:
        owner = make_user(role="owner")
        stranger = make_user(role="owner")
        rental = _property(client, owner)
        resp = _upload(client, stranger, rental["id"], _ics(_stay(5, "a")))
        assert resp.status_code == 404

    def test_something_that_is_not_a_calendar_is_named(
        self, client: TestClient, make_user
    ) -> None:
        """An owner who uploaded a PDF of their bookings deserves to be told
        that is what happened, rather than getting an empty list."""
        owner = make_user(role="owner")
        rental = _property(client, owner)
        resp = client.post(
            f"/api/properties/{rental['id']}/calendars/read-file",
            files={"file": ("bookings.pdf", b"\x89PNG\r\n\x1a\n\xff\xfe", "application/pdf")},
            headers=owner["auth"],
        )
        assert resp.status_code == 400
        assert "calendar file" in resp.json()["detail"]

    def test_an_empty_file_is_named(
        self, client: TestClient, make_user
    ) -> None:
        owner = make_user(role="owner")
        rental = _property(client, owner)
        resp = _upload(client, owner, rental["id"], "")
        assert resp.status_code == 400

    def test_a_file_past_the_cap_refuses_without_reading_it_all(
        self, client: TestClient, make_user
    ) -> None:
        """The same limit the fetch path enforces while streaming, and for the
        same reason: reading the whole thing to find out how big it is is not
        a limit at all."""
        from app.services import calendars

        owner = make_user(role="owner")
        rental = _property(client, owner)
        resp = _upload(
            client, owner, rental["id"], "x" * (calendars.MAX_FEED_BYTES + 10)
        )
        assert resp.status_code == 413

    def test_bookings_seen_explains_an_empty_list(
        self, client: TestClient, make_user
    ) -> None:
        """A file of last year's bookings is read and then dropped by the past
        floor. Returning an unexplained empty list would look like a broken
        upload rather than an old calendar."""
        from datetime import date

        owner = make_user(role="owner")
        rental = _property(client, owner)
        long_ago = date.today() - timedelta(days=400)
        resp = _upload(
            client,
            owner,
            rental["id"],
            _ics(
                _event(
                    "old",
                    long_ago.strftime("%Y%m%d"),
                    (long_ago + timedelta(days=3)).strftime("%Y%m%d"),
                )
            ),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["bookings_seen"] == 1
        assert body["jobs"] == []

    def test_the_upload_reaches_no_network(
        self, client: TestClient, make_user, monkeypatch
    ) -> None:
        """The one way to read a calendar here without this server connecting
        anywhere — so none of the request-forgery surface a URL carries."""
        import httpx

        def explode(*args, **kwargs):
            raise AssertionError("the upload path opened a connection")

        # `HTTPTransport`, not `Client.send`: the test client is itself built
        # on httpx and drives the app through `ASGITransport`, so patching the
        # client would only break the request under test. This is the layer
        # that reaches a real socket, which is what the claim is about.
        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", explode)
        monkeypatch.setattr(
            calendars_module, "fetch", explode
        )

        owner = make_user(role="owner")
        rental = _property(client, owner)
        resp = _upload(client, owner, rental["id"], _ics(_stay(5, "a")))
        assert resp.status_code == 200, resp.text


class TestPagingOverATotalOrder:
    """`offset` is only meaningful over an order SQL actually defines.

    Ordering by `checkout_at` alone leaves rows sharing an instant in an
    undefined order, and nothing obliges the database to pick the same one
    twice — so two pages can repeat a row and skip another, putting a job on no
    page at all. That is the same failure the paging was added to fix, one
    level down.

    Ties are not exotic here: an owner with several properties on the same
    default checkout hour makes them by the dozen, and `create_many` writes a
    season of them at once.

    **These assert the order rather than comparing two pages**, which is the
    only honest way to test this. Postgres is free to return ties in any order
    and in practice returns them consistently, so paging twice and finding the
    same rows passes just as well without the tiebreaker as with it — the first
    version of this test did exactly that and proved nothing. Ids are random
    uuid4s, so an id-ascending tie group is not something an arbitrary plan
    produces by accident.
    """

    def _tied_jobs(self, client: TestClient, owner: dict, count: int) -> str:
        when = (datetime.now(timezone.utc) + timedelta(days=12)).isoformat()
        for _ in range(count):
            prop = _property(client, owner)
            created = client.post(
                "/api/turnovers/bulk",
                json={"property_id": prop["id"], "jobs": [{"checkout_at": when}]},
                headers=owner["auth"],
            )
            assert created.status_code == 201, created.text
        return when

    def test_rows_sharing_an_instant_come_back_in_id_order(
        self, client: TestClient, make_user
    ) -> None:
        owner = make_user(role="owner")
        self._tied_jobs(client, owner, 10)

        rows = client.get("/api/turnovers?limit=50", headers=owner["auth"]).json()
        ids = [row["id"] for row in rows]
        assert len(ids) == 10
        assert ids == sorted(ids), (
            "ties came back in an order the database chose, which it is free to "
            "choose differently next time — so a row can fall between two pages"
        )

    def test_every_row_appears_on_exactly_one_page(
        self, client: TestClient, make_user
    ) -> None:
        """The failure this prevents, stated as the owner would meet it."""
        owner = make_user(role="owner")
        self._tied_jobs(client, owner, 10)

        seen: list[str] = []
        for offset in range(0, 10, 3):
            page = client.get(
                f"/api/turnovers?limit=3&offset={offset}", headers=owner["auth"]
            )
            assert page.status_code == 200, page.text
            seen.extend(row["id"] for row in page.json())

        assert len(seen) == 10
        assert len(set(seen)) == 10, "a row appeared twice, so another is on no page"
        # And the pages, stitched together, are still one total order.
        assert seen == sorted(seen)
