"""The cleaner is on the way — the one job signal about the future.

`started_at` and `completed_at` both report something that has already
happened. Neither can answer the question an owner actually asks on the morning
of a turnover, which is whether anybody is coming, and the fallback for that is
the phone call this product exists to replace.

So it needed a sixteenth `NotificationEvent`, which the closed list makes a
deliberate act rather than a string appearing in one call site. These tests are
the other half of that rule from `notification-completeness`: **a transition is
not done until its notification has both a sender and a test that would fail if
the send were deleted.**

What is stored is a timestamp somebody wrote by pressing a button. There is no
coordinate here and deliberately none anywhere — continuous location on an
independent contractor is a different product with its own consent, retention
and disclosure questions.
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Notification, Turnover
from app.models.enums import NotificationEvent


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
    job["award"] = accepted.json()["award"]
    return job


def _rows(db: Session, event: NotificationEvent) -> list[Notification]:
    db.expire_all()
    return list(
        db.execute(select(Notification).where(Notification.event == event))
        .scalars()
        .all()
    )


def _on_my_way(client: TestClient, job: dict):
    return client.post(
        f"/api/board/jobs/{job['turnover']['id']}/on-my-way",
        headers=job["cleaner"]["auth"],
    )


class TestTheOwnerIsTold:
    def test_the_owner_gets_a_notification(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        """**The test the closed list exists to require.** Delete the `queue`
        call in `notifications.cleaner_en_route` and this fails — which is the
        only thing that makes a missing notification loud, since every other
        part of the product carries on working perfectly without it."""
        job = _award_a_job(client, make_cleaner, make_open_turnover)

        resp = _on_my_way(client, job)

        assert resp.status_code == 200, resp.text
        assert resp.json()["en_route_at"] is not None

        rows = _rows(db, NotificationEvent.CLEANER_EN_ROUTE)
        assert len(rows) == 1
        turnover = db.get(Turnover, uuid.UUID(job["turnover"]["id"]))
        assert rows[0].recipient_id == turnover.property.owner_id

    def test_nobody_else_hears_about_it(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session,
        admin_user,
    ) -> None:
        """No admin copy. This is an ordinary job going ordinarily well, and an
        inbox that receives every cleaner leaving the house is an inbox nobody
        reads the cancellations in."""
        job = _award_a_job(client, make_cleaner, make_open_turnover)

        _on_my_way(client, job)

        rows = _rows(db, NotificationEvent.CLEANER_EN_ROUTE)
        assert len(rows) == 1

    def test_pressing_it_twice_keeps_the_first_time_and_sends_once(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        """A second tap on a phone in a driveway is not a correction.

        Re-stamping would move "left at 9:05" to whenever they last fidgeted
        with the screen, and a second message saying the same thing is how a
        useful alert becomes one the owner mutes.
        """
        job = _award_a_job(client, make_cleaner, make_open_turnover)

        first = _on_my_way(client, job).json()["en_route_at"]
        second = _on_my_way(client, job).json()["en_route_at"]

        assert first == second
        assert len(_rows(db, NotificationEvent.CLEANER_EN_ROUTE)) == 1


class TestWhoMaySayIt:
    def test_a_cleaner_who_is_not_booked_on_it_cannot(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        """Telling somebody's owner that you are on the way to their house is
        not a thing a passer-by gets to do."""
        job = _award_a_job(client, make_cleaner, make_open_turnover)
        intruder = make_cleaner(cleared=True)

        resp = client.post(
            f"/api/board/jobs/{job['turnover']['id']}/on-my-way",
            headers=intruder["auth"],
        )

        assert resp.status_code == 404
        assert _rows(db, NotificationEvent.CLEANER_EN_ROUTE) == []

    def test_an_owner_cannot_say_it_for_them(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        job = _award_a_job(client, make_cleaner, make_open_turnover)

        resp = client.post(
            f"/api/board/jobs/{job['turnover']['id']}/on-my-way",
            headers=job["owner"]["auth"],
        )

        assert resp.status_code == 403


class TestWhenItNoLongerMeansAnything:
    def test_a_cancelled_booking_refuses_it(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        """Nobody is coming, and saying so to the owner would be worse than
        silence — they would stop looking for a replacement."""
        job = _award_a_job(client, make_cleaner, make_open_turnover)
        client.post(
            f"/api/board/jobs/{job['turnover']['id']}/cancel",
            json={"reason": "van broke down"},
            headers=job["cleaner"]["auth"],
        )

        resp = _on_my_way(client, job)

        assert resp.status_code in (404, 409)
        assert _rows(db, NotificationEvent.CLEANER_EN_ROUTE) == []

    def test_a_finished_job_refuses_it(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        job = _award_a_job(client, make_cleaner, make_open_turnover)
        client.post(
            f"/api/board/jobs/{job['turnover']['id']}/complete",
            headers=job["cleaner"]["auth"],
        )

        resp = _on_my_way(client, job)

        assert resp.status_code == 409
        assert _rows(db, NotificationEvent.CLEANER_EN_ROUTE) == []

    def test_saying_it_late_is_allowed(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        """A cleaner who forgot on the way and taps it on the doorstep has told
        the truth late. Refusing them means the owner learns nothing at all,
        which is worse than learning it a minute after arrival."""
        job = _award_a_job(client, make_cleaner, make_open_turnover)
        client.post(
            f"/api/board/jobs/{job['turnover']['id']}/start",
            headers=job["cleaner"]["auth"],
        )

        resp = _on_my_way(client, job)

        assert resp.status_code == 200, resp.text
        assert len(_rows(db, NotificationEvent.CLEANER_EN_ROUTE)) == 1


class TestTheOwnerCanSeeIt:
    def test_it_is_on_the_turnover_the_owner_reads(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        """A timestamp nobody's screen reads is a timestamp that does not
        exist. The email is the push; this is where they look afterwards."""
        job = _award_a_job(client, make_cleaner, make_open_turnover)
        _on_my_way(client, job)

        detail = client.get(
            f"/api/turnovers/{job['turnover']['id']}", headers=job["owner"]["auth"]
        )

        assert detail.status_code == 200, detail.text
        assert detail.json()["award"]["en_route_at"] is not None

    def test_it_is_not_on_anything_a_bidding_cleaner_can_see(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        """The privacy boundary does not move for this. Whether somebody is on
        their way to a house is adjacent to whether anybody is home."""
        job = _award_a_job(client, make_cleaner, make_open_turnover)
        _on_my_way(client, job)
        onlooker = make_cleaner(cleared=True)

        board = client.get("/api/board", headers=onlooker["auth"])

        assert board.status_code == 200, board.text
        assert "en_route" not in board.text
