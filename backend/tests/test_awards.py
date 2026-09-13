"""Awarding a turnover — and guardrail 1, tested for real.

The carry-over list has had one entry since day one that this file exists to
satisfy: *two simultaneous accepts on two different bids for the same turnover,
exactly one wins*. Two tests here, because "exactly one wins" on its own is not
enough evidence:

* `TestTwoAcceptsAtOnce` fires both requests from two threads through two
  connections and asserts one 200 and one 409. It is the outcome the product
  promises.
* `TestTheLockIsReal` proves the *mechanism*. A check-then-lock implementation
  can pass a race test by luck — the threads may simply not interleave on the
  run that matters. So this one takes `SELECT ... FOR UPDATE` on the turnover
  row from the test's own connection and shows that an accept then blocks until
  the test lets go. A route that checked first and locked later would sail past
  that lock and answer immediately.

Both need real connections and real row locks, which is why the suite runs on
PostgreSQL and not SQLite (see conftest).
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal, get_db
from app.main import app
from app.models import Award, Bid, BidStatus, Turnover, TurnoverStatus


def _bid(client: TestClient, cleaner: dict, turnover_id: str, cents: int) -> dict:
    resp = client.put(
        f"/api/board/{turnover_id}/bid",
        json={"price_cents": cents, "message": "Happy to take it."},
        headers=cleaner["auth"],
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestAcceptingABid:
    def test_accepting_books_the_cleaner_and_closes_the_job(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        job = make_open_turnover()
        cleaner = make_cleaner(cleared=True)
        bid = _bid(client, cleaner, job["turnover"]["id"], 13_000)

        resp = client.post(
            f"/api/turnovers/{job['turnover']['id']}/bids/{bid['id']}/accept",
            headers=job["owner"]["auth"],
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert body["status"] == "awarded"
        assert body["award"]["agreed_price_cents"] == 13_000
        assert body["award"]["cleaner_name"] == cleaner["user"]["full_name"]
        assert body["award"]["cancelled_at"] is None
        # The one-shape rule: an action answers with everything the GET gave.
        assert body["property"]["nickname"] == job["property"]["nickname"]

    def test_the_price_is_frozen_at_the_bid(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        """The agreed price is the money side's input. It cannot drift later."""
        job = make_open_turnover()
        cleaner = make_cleaner(cleared=True)
        bid = _bid(client, cleaner, job["turnover"]["id"], 9_900)

        client.post(
            f"/api/turnovers/{job['turnover']['id']}/bids/{bid['id']}/accept",
            headers=job["owner"]["auth"],
        )

        award = db.execute(
            select(Award).where(Award.turnover_id == uuid.UUID(job["turnover"]["id"]))
        ).scalar_one()
        assert award.agreed_price_cents == 9_900
        assert award.bid_id == uuid.UUID(bid["id"])

    def test_everyone_else_is_declined(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        job = make_open_turnover()
        winner, loser = make_cleaner(cleared=True), make_cleaner(cleared=True)
        winning_bid = _bid(client, winner, job["turnover"]["id"], 11_000)
        _bid(client, loser, job["turnover"]["id"], 15_000)

        client.post(
            f"/api/turnovers/{job['turnover']['id']}/bids/{winning_bid['id']}/accept",
            headers=job["owner"]["auth"],
        )

        statuses = {
            bid["id"]: bid["status"]
            for bid in client.get(
                f"/api/turnovers/{job['turnover']['id']}/bids",
                headers=job["owner"]["auth"],
            ).json()
        }
        assert statuses[winning_bid["id"]] == "accepted"
        assert set(statuses.values()) == {"accepted", "declined"}

        mine = client.get("/api/board/bids/mine", headers=loser["auth"]).json()
        assert mine[0]["status"] == "declined", "a losing bidder is told, not left pending"

    def test_a_second_accept_on_the_same_turnover_is_refused(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        """The sequential case. The concurrent one is further down this file."""
        job = make_open_turnover()
        first, second = make_cleaner(cleared=True), make_cleaner(cleared=True)
        first_bid = _bid(client, first, job["turnover"]["id"], 12_000)
        second_bid = _bid(client, second, job["turnover"]["id"], 12_500)

        ok = client.post(
            f"/api/turnovers/{job['turnover']['id']}/bids/{first_bid['id']}/accept",
            headers=job["owner"]["auth"],
        )
        assert ok.status_code == 200

        again = client.post(
            f"/api/turnovers/{job['turnover']['id']}/bids/{second_bid['id']}/accept",
            headers=job["owner"]["auth"],
        )
        assert again.status_code == 409
        assert "not taking bids" in again.json()["detail"]

    def test_a_cleaner_whose_clearance_lapsed_cannot_be_accepted(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        """The bidding gate is re-read at accept time, from the same place.

        A background check that came back rejected after the bid was placed must
        not be able to walk into a house today.
        """
        from app.models import VerificationStatus

        job = make_open_turnover()
        cleaner = make_cleaner(cleared=True)
        bid = _bid(client, cleaner, job["turnover"]["id"], 10_000)

        cleaner["profile"].background_check_status = VerificationStatus.REJECTED
        db.commit()

        resp = client.post(
            f"/api/turnovers/{job['turnover']['id']}/bids/{bid['id']}/accept",
            headers=job["owner"]["auth"],
        )
        assert resp.status_code == 409
        assert "no longer cleared" in resp.json()["detail"]
        assert (
            db.execute(
                select(Award).where(Award.turnover_id == uuid.UUID(job["turnover"]["id"]))
            ).scalar_one_or_none()
            is None
        )

    def test_another_owners_turnover_answers_404(
        self, client: TestClient, make_cleaner, make_open_turnover, make_user
    ) -> None:
        job = make_open_turnover()
        cleaner = make_cleaner(cleared=True)
        bid = _bid(client, cleaner, job["turnover"]["id"], 10_000)
        stranger = make_user(role="owner")

        resp = client.post(
            f"/api/turnovers/{job['turnover']['id']}/bids/{bid['id']}/accept",
            headers=stranger["auth"],
        )
        assert resp.status_code == 404, "403 would confirm the id exists"


class TestDecliningABid:
    def test_declining_leaves_the_job_open(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        job = make_open_turnover()
        cleaner = make_cleaner(cleared=True)
        bid = _bid(client, cleaner, job["turnover"]["id"], 30_000)

        resp = client.post(
            f"/api/turnovers/{job['turnover']['id']}/bids/{bid['id']}/decline",
            headers=job["owner"]["auth"],
        )
        assert resp.status_code == 200
        assert resp.json()[0]["status"] == "declined"

        turnover = client.get(
            f"/api/turnovers/{job['turnover']['id']}", headers=job["owner"]["auth"]
        ).json()
        assert turnover["status"] == "open"
        assert turnover["award"] is None

    def test_an_accepted_bid_cannot_be_declined_out_from_under_the_cleaner(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        job = make_open_turnover()
        cleaner = make_cleaner(cleared=True)
        bid = _bid(client, cleaner, job["turnover"]["id"], 10_000)
        client.post(
            f"/api/turnovers/{job['turnover']['id']}/bids/{bid['id']}/accept",
            headers=job["owner"]["auth"],
        )

        resp = client.post(
            f"/api/turnovers/{job['turnover']['id']}/bids/{bid['id']}/decline",
            headers=job["owner"]["auth"],
        )
        assert resp.status_code == 409
        assert "Cancel the turnover instead" in resp.json()["detail"]


@pytest.fixture
def own_session_per_request() -> Iterator[None]:
    """Give every request its own session, as production does.

    The `client` fixture deliberately shares the test's session so a test can
    read back what a request wrote. That sharing makes a concurrency test
    meaningless — two requests on one connection cannot race — so these tests
    swap in the real thing for the duration.
    """

    def _get_db() -> Iterator[Session]:
        session = SessionLocal()
        try:
            yield session
        finally:
            session.close()

    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = _get_db
    try:
        yield
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous


class TestTwoAcceptsAtOnce:
    """The carry-over test, written down on day one and built with its phase."""

    def test_exactly_one_of_two_simultaneous_accepts_wins(
        self,
        client: TestClient,
        make_cleaner,
        make_open_turnover,
        db: Session,
        own_session_per_request,
    ) -> None:
        job = make_open_turnover()
        first, second = make_cleaner(cleared=True), make_cleaner(cleared=True)
        first_bid = _bid(client, first, job["turnover"]["id"], 12_000)
        second_bid = _bid(client, second, job["turnover"]["id"], 12_500)
        turnover_id = job["turnover"]["id"]
        auth = job["owner"]["auth"]

        # Release the setup session's snapshot and any locks it holds, so the
        # two racing connections are the only ones in play.
        db.commit()

        start = threading.Barrier(2)
        results: dict[str, int] = {}

        def accept(name: str, bid_id: str) -> None:
            # A client each: one portal per thread, one connection per request.
            with TestClient(app) as racer:
                start.wait(timeout=10)
                resp = racer.post(
                    f"/api/turnovers/{turnover_id}/bids/{bid_id}/accept", headers=auth
                )
                results[name] = resp.status_code

        threads = [
            threading.Thread(target=accept, args=("first", first_bid["id"])),
            threading.Thread(target=accept, args=("second", second_bid["id"])),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            assert not thread.is_alive(), "an accept never returned — deadlock?"

        assert sorted(results.values()) == [200, 409], results

        # And the database agrees: one live award, one cleaner, one awarded job.
        db.expire_all()
        live = (
            db.execute(
                select(Award).where(
                    Award.turnover_id == uuid.UUID(turnover_id),
                    Award.cancelled_at.is_(None),
                )
            )
            .scalars()
            .all()
        )
        assert len(live) == 1, "two cleaners were promised one cleaning"

        turnover = db.get(Turnover, uuid.UUID(turnover_id))
        assert turnover is not None
        assert turnover.status is TurnoverStatus.AWARDED

        accepted = (
            db.execute(
                select(Bid).where(
                    Bid.turnover_id == uuid.UUID(turnover_id),
                    Bid.status == BidStatus.ACCEPTED,
                )
            )
            .scalars()
            .all()
        )
        assert len(accepted) == 1
        assert accepted[0].cleaner_id == live[0].cleaner_id


class TestTheLockIsReal:
    """Evidence that the check happens *inside* the lock, not before it.

    "The request blocked" on its own proves nothing: an implementation that
    checks first and locks later blocks too — at its final write, long after it
    has made up its mind. So this test changes the answer while the request is
    waiting. Another connection takes the row lock, the accept goes in behind
    it, and only then is the turnover awarded to somebody else and the lock
    released.

    Locking first, the route reads the row *after* it is granted the lock, sees
    the turnover already awarded, and refuses: 409. Checking first, it decided
    "not awarded" before any of that happened and goes on to write regardless —
    which the partial unique index then rejects as a 500, the loud failure the
    index exists to produce. Either way the difference is visible here.
    """

    def test_an_accept_rechecks_under_the_lock(
        self,
        client: TestClient,
        make_cleaner,
        make_open_turnover,
        db: Session,
        own_session_per_request,
    ) -> None:
        job = make_open_turnover()
        cleaner = make_cleaner(cleared=True)
        rival = make_cleaner(cleared=True)
        bid = _bid(client, cleaner, job["turnover"]["id"], 12_000)
        turnover_id = uuid.UUID(job["turnover"]["id"])
        rival_id = uuid.UUID(rival["user"]["id"])
        auth = job["owner"]["auth"]
        db.commit()

        holder = SessionLocal()
        finished = threading.Event()
        outcome: dict[str, int] = {}

        try:
            locked = holder.execute(
                select(Turnover).where(Turnover.id == turnover_id).with_for_update()
            ).scalar_one()

            def accept() -> None:
                with TestClient(app) as racer:
                    resp = racer.post(
                        f"/api/turnovers/{turnover_id}/bids/{bid['id']}/accept",
                        headers=auth,
                    )
                    outcome["status"] = resp.status_code
                finished.set()

            thread = threading.Thread(target=accept)
            thread.start()

            assert not finished.wait(timeout=2.0), (
                "the accept answered while another connection held the row lock"
            )

            # The world changes while the request waits: somebody else got it.
            holder.add(
                Award(turnover_id=turnover_id, cleaner_id=rival_id, agreed_price_cents=11_000)
            )
            locked.status = TurnoverStatus.AWARDED
            holder.commit()  # releases the lock

            assert finished.wait(timeout=30), "the accept never returned after the lock lifted"
            thread.join(timeout=5)
            assert outcome["status"] == 409, (
                "the accept answered from a check it made before taking the lock"
            )
        finally:
            holder.rollback()
            holder.close()

        db.expire_all()
        live = (
            db.execute(
                select(Award).where(
                    Award.turnover_id == turnover_id, Award.cancelled_at.is_(None)
                )
            )
            .scalars()
            .all()
        )
        assert len(live) == 1
        assert live[0].cleaner_id == rival_id
