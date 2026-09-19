"""Was the cleaner actually at the property when they said they were?

**One reading, at the arrival tap, turned into a distance and thrown away.**
The whole design is in what is *not* stored: no coordinates, no trail, nothing
that can be replayed into where somebody was. What survives is how far from a
point the owner already knows their phone claimed to be — a ring, not a place.

Two things these tests exist to hold:

1. **Three answers, not two.** The obvious version is `confirmed`/`away`, which
   forces every reading it could not take — permission refused, no GPS, a
   desktop browser, a property with no coordinates, a fix too coarse to mean
   anything — into one of those. Both are lies, and `away` is the worse one: it
   accuses somebody on the strength of a missing browser permission.
2. **It is a claim, not proof.** The coordinate comes from the cleaner's own
   browser. Nothing in the product acts on it automatically, and the failure
   mode worth guarding is somebody treating a green tick as evidence in a
   dispute it cannot settle.
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Award, Turnover
from app.services import awards

#: The fixture puts every property here.
PORTLAND = (43.6591, -70.2568)


def _award_a_job(client: TestClient, make_cleaner, make_open_turnover, **kwargs) -> dict:
    job = make_open_turnover(**kwargs)
    cleaner = make_cleaner(cleared=True)
    bid = client.put(
        f"/api/board/{job['turnover']['id']}/bid",
        json={"price_cents": 12_000},
        headers=cleaner["auth"],
    )
    assert bid.status_code == 200, bid.text
    accepted = client.post(
        f"/api/turnovers/{job['turnover']['id']}/bids/{bid.json()['id']}/accept",
        headers=job["owner"]["auth"],
    )
    assert accepted.status_code == 200, accepted.text
    job["cleaner"] = cleaner
    return job


def _arrive(client: TestClient, job: dict, body=None):
    return client.post(
        f"/api/board/jobs/{job['turnover']['id']}/start",
        json=body,
        headers=job["cleaner"]["auth"],
    )


def _award_row(db: Session, job: dict) -> Award:
    db.expire_all()
    turnover = db.get(Turnover, uuid.UUID(job["turnover"]["id"]))
    return turnover.live_award


class TestTheThreeAnswers:
    def test_at_the_property_is_confirmed(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        job = _award_a_job(client, make_cleaner, make_open_turnover)

        resp = _arrive(
            client,
            job,
            {"lat": PORTLAND[0], "lng": PORTLAND[1], "accuracy_m": 12},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["arrival_check"] == awards.ARRIVAL_CONFIRMED
        assert resp.json()["arrival_distance_m"] < awards.ARRIVAL_WITHIN_M

    def test_miles_away_is_away(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        """The case the feature is for — and the number comes back with it, so
        "away" is something an owner can judge rather than an accusation with
        nothing behind it."""
        job = _award_a_job(client, make_cleaner, make_open_turnover)

        # Portsmouth, about 45 miles down the coast.
        resp = _arrive(
            client, job, {"lat": 43.0718, "lng": -70.7626, "accuracy_m": 12}
        )

        assert resp.json()["arrival_check"] == awards.ARRIVAL_AWAY
        assert resp.json()["arrival_distance_m"] > 50_000

    def test_no_reading_at_all_is_unchecked(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        """**A refused permission is not evidence of anything.** This is the
        common case, not the edge one: a cleaner on a desktop, in a basement,
        or who simply said no to the prompt."""
        job = _award_a_job(client, make_cleaner, make_open_turnover)

        resp = _arrive(client, job)

        assert resp.status_code == 200, resp.text
        assert resp.json()["arrival_check"] == awards.ARRIVAL_UNCHECKED
        assert resp.json()["arrival_distance_m"] is None

    def test_a_fix_too_coarse_to_mean_anything_is_unchecked(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        """**The one that is easy to miss.**

        A browser falling back to IP geolocation returns a position accurate to
        tens of kilometres. Land one of those inside the radius and a
        two-valued check says `confirmed` — making the tick most trustworthy
        exactly where the data is worst.
        """
        job = _award_a_job(client, make_cleaner, make_open_turnover)

        resp = _arrive(
            client,
            job,
            {"lat": PORTLAND[0], "lng": PORTLAND[1], "accuracy_m": 30_000},
        )

        assert resp.json()["arrival_check"] == awards.ARRIVAL_UNCHECKED

    def test_a_position_without_its_accuracy_is_unchecked(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        """All three fields or none. Defaulting the accuracy to something
        optimistic would be the endpoint manufacturing the confidence the
        column exists to record honestly."""
        job = _award_a_job(client, make_cleaner, make_open_turnover)

        resp = _arrive(client, job, {"lat": PORTLAND[0], "lng": PORTLAND[1]})

        assert resp.status_code == 200, resp.text
        assert resp.json()["arrival_check"] == awards.ARRIVAL_UNCHECKED

    def test_a_property_with_no_coordinates_is_unchecked(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        """Geocoding is best-effort and an owner can save an address it could
        not place. There is nothing to measure against, which is not the
        cleaner's fault and must not read as though it were."""
        job = _award_a_job(client, make_cleaner, make_open_turnover)
        turnover = db.get(Turnover, uuid.UUID(job["turnover"]["id"]))
        turnover.property.lat = None
        turnover.property.lng = None
        db.commit()

        resp = _arrive(
            client,
            job,
            {"lat": PORTLAND[0], "lng": PORTLAND[1], "accuracy_m": 12},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["arrival_check"] == awards.ARRIVAL_UNCHECKED


class TestWhatIsNotKept:
    def test_no_coordinate_is_ever_stored(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        """**The promise the whole feature rests on.** A distance is a ring
        around a point the owner already knows; a coordinate is a place, and
        two of them are a trail. The landing page says this in words; this is
        the same sentence asserted against the schema."""
        job = _award_a_job(client, make_cleaner, make_open_turnover)
        _arrive(
            client,
            job,
            {"lat": PORTLAND[0], "lng": PORTLAND[1], "accuracy_m": 12},
        )

        columns = {c.name for c in Award.__table__.columns}
        for forbidden in ("lat", "lng", "latitude", "longitude", "location", "point"):
            assert forbidden not in columns

        award = _award_row(db, job)
        stored = {
            getattr(award, c.name)
            for c in Award.__table__.columns
            if isinstance(getattr(award, c.name), (int, float))
        }
        # The reading itself must not survive in any column, under any name.
        assert not any(
            isinstance(v, float) and abs(v - PORTLAND[0]) < 0.01 for v in stored
        )

    def test_arriving_twice_keeps_the_first_measurement(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        """A second tap is not a second arrival. Letting it overwrite would let
        somebody who turned up late re-record themselves from the doorstep."""
        job = _award_a_job(client, make_cleaner, make_open_turnover)
        _arrive(client, job, {"lat": 43.0718, "lng": -70.7626, "accuracy_m": 12})

        again = _arrive(
            client,
            job,
            {"lat": PORTLAND[0], "lng": PORTLAND[1], "accuracy_m": 12},
        )

        assert again.json()["arrival_check"] == awards.ARRIVAL_AWAY


class TestWhoSeesIt:
    def test_the_owner_sees_the_verdict_on_their_turnover(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        job = _award_a_job(client, make_cleaner, make_open_turnover)
        _arrive(
            client,
            job,
            {"lat": PORTLAND[0], "lng": PORTLAND[1], "accuracy_m": 12},
        )

        detail = client.get(
            f"/api/turnovers/{job['turnover']['id']}", headers=job["owner"]["auth"]
        )

        assert detail.status_code == 200, detail.text
        assert detail.json()["award"]["arrival_check"] == awards.ARRIVAL_CONFIRMED

    def test_the_cleaner_sees_what_was_recorded_about_them(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        """Basic fairness: somebody should be able to see the thing that might
        later be held against them, on the screen where it was recorded."""
        job = _award_a_job(client, make_cleaner, make_open_turnover)
        _arrive(client, job, {"lat": 43.0718, "lng": -70.7626, "accuracy_m": 12})

        jobs = client.get("/api/board/jobs", headers=job["cleaner"]["auth"])

        mine = [
            j for j in jobs.json() if j["turnover_id"] == job["turnover"]["id"]
        ]
        assert mine and mine[0]["arrival_check"] == awards.ARRIVAL_AWAY

    def test_it_is_on_nothing_a_bidding_cleaner_can_see(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        job = _award_a_job(client, make_cleaner, make_open_turnover)
        _arrive(
            client,
            job,
            {"lat": PORTLAND[0], "lng": PORTLAND[1], "accuracy_m": 12},
        )
        onlooker = make_cleaner(cleared=True)

        board = client.get("/api/board", headers=onlooker["auth"])

        assert "arrival" not in board.text


class TestItIsOnlyEverAClaim:
    def test_nothing_in_the_product_acts_on_it(self) -> None:
        """**The rule worth a test rather than a comment.**

        A browser coordinate can be fabricated by anybody who wants to, so a
        verdict here may not gate money, cancel a booking, flag a no-show, or
        feed a rating. It informs a person. If a future change wires it into
        one of those, this fails and somebody has to say out loud that they are
        treating a spoofable claim as proof.
        """
        from pathlib import Path

        #: Where it is decided, the model property that delegates to it, the
        #: two response shapes, and the route that reads it on to a screen.
        EXPECTED = {
            "services/awards.py",
            "models/award.py",
            "schemas/award.py",
            "schemas/board.py",
            "api/routes/board.py",
        }

        root = Path(__file__).resolve().parents[1] / "app"
        readers = {
            path.relative_to(root).as_posix()
            for path in root.rglob("*.py")
            if "arrival_check" in path.read_text(encoding="utf-8")
            or "arrival_distance_m" in path.read_text(encoding="utf-8")
        }

        assert readers == EXPECTED, (
            f"new reader(s) of the arrival check: {sorted(readers - EXPECTED)}. "
            "It is a claim from an untrusted browser, so it may inform a "
            "person and must not gate money, a cancellation, a no-show flag "
            "or a rating."
        )
