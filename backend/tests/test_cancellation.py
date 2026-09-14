"""The no-show and late-cancellation path.

The other carry-over test written down on day one: *a turnover reopens urgent,
owner and admin both alerted*. Three ways a booking ends, and the rule is the
same for all of them — the award is cancelled rather than erased, the job goes
back where it can be re-staffed, and nobody finds out by showing up:

* the cleaner backs out,
* the owner calls the job off (the exception: it does not go back on the bench,
  because the owner does not want it done),
* the cleaner never turns up at all.

"Alerted" is checked against the `notifications` table. Phase 4 asserted on a log
line, because it had no delivery; phase 5 gives it a row, which is a stronger
test of the same promise — a log line proves a decision was made, a row proves
there is a record somebody can point at a week later when they say nobody told
them.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Award,
    Bid,
    BidStatus,
    Notification,
    Turnover,
    TurnoverStatus,
    TurnoverUrgency,
)


def _award_a_job(client: TestClient, make_cleaner, make_open_turnover, **kwargs) -> dict:
    """Post a job, take a bid on it, and accept — the state everything starts from."""
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
    job["bid"] = bid.json()
    job["award"] = accepted.json()["award"]
    return job


def _notified(db: Session, event: str) -> list[Notification]:
    """Every notification recorded for one event, freshest read from the row."""
    db.expire_all()
    return list(
        db.execute(
            select(Notification).where(Notification.event == event)
        )
        .scalars()
        .all()
    )


def _recipients(db: Session, event: str) -> set[str]:
    return {row.destination for row in _notified(db, event)}


class TestACleanerBacksOut:
    def test_the_job_goes_back_on_the_bench(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        job = _award_a_job(client, make_cleaner, make_open_turnover)

        resp = client.post(
            f"/api/board/jobs/{job['turnover']['id']}/cancel",
            json={"reason": "My van is off the road."},
            headers=job["cleaner"]["auth"],
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert body["status"] == "open", "the owner still needs this cleaned"
        assert body["cancelled_at"] is not None
        assert body["was_no_show"] is False

        turnover = db.get(Turnover, uuid.UUID(job["turnover"]["id"]))
        db.refresh(turnover)
        assert turnover.status is TurnoverStatus.OPEN
        assert turnover.reopened_at is not None

    def test_a_late_cancellation_makes_the_repost_urgent(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        """The rung that matters: a wide cleaning window, hours before checkout.

        Judged on the window alone this is `standard` — four days between
        checkout and the next checkin. But nobody is staffed for it and checkout
        is six hours away, so the lead time is what a cleaner scanning the board
        needs to see.
        """
        job = _award_a_job(client, make_cleaner, make_open_turnover, days_out=0)

        turnover = db.get(Turnover, uuid.UUID(job["turnover"]["id"]))
        turnover.checkout_at = datetime.now(timezone.utc) + timedelta(hours=6)
        turnover.checkin_at = turnover.checkout_at + timedelta(days=4)
        db.commit()

        before = client.get(
            f"/api/turnovers/{job['turnover']['id']}", headers=job["owner"]["auth"]
        ).json()
        assert before["urgency"] == "standard", "the window alone says standard"

        client.post(
            f"/api/board/jobs/{job['turnover']['id']}/cancel",
            json={"reason": "Family emergency."},
            headers=job["cleaner"]["auth"],
        )

        after = client.get(
            f"/api/turnovers/{job['turnover']['id']}", headers=job["owner"]["auth"]
        ).json()
        assert after["urgency"] == "urgent"
        assert after["status"] == "open"

    def test_the_owner_and_an_admin_are_both_told(
        self, client: TestClient, make_cleaner, make_open_turnover, admin_user, db: Session
    ) -> None:
        job = _award_a_job(client, make_cleaner, make_open_turnover)

        client.post(
            f"/api/board/jobs/{job['turnover']['id']}/cancel",
            json={"reason": "Double booked myself."},
            headers=job["cleaner"]["auth"],
        )

        told = _recipients(db, "cleaner_cancelled")
        assert job["owner"]["user"]["email"] in told, "the owner was not told"
        assert admin_user["user"].email in told, "no admin was told"
        assert job["cleaner"]["user"]["email"] in told, "the cleaner has no record of it"

        # And the message says what happened, not just that something did.
        body = _notified(db, "cleaner_cancelled")[0].body
        assert "Double booked myself." in body
        assert "back on the bench" in body

    def test_the_job_is_biddable_again_and_can_be_re_awarded(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        """The whole point of re-posting: somebody else can actually take it."""
        job = _award_a_job(client, make_cleaner, make_open_turnover)
        client.post(
            f"/api/board/jobs/{job['turnover']['id']}/cancel",
            json={"reason": "Cannot make it."},
            headers=job["cleaner"]["auth"],
        )

        replacement = make_cleaner(cleared=True)
        new_bid = client.put(
            f"/api/board/{job['turnover']['id']}/bid",
            json={"price_cents": 14_000},
            headers=replacement["auth"],
        )
        assert new_bid.status_code == 200, new_bid.text

        accepted = client.post(
            f"/api/turnovers/{job['turnover']['id']}/bids/{new_bid.json()['id']}/accept",
            headers=job["owner"]["auth"],
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["award"]["agreed_price_cents"] == 14_000

        awards = (
            db.execute(
                select(Award).where(Award.turnover_id == uuid.UUID(job["turnover"]["id"]))
            )
            .scalars()
            .all()
        )
        assert len(awards) == 2, "the cleaner who backed out is still on the record"
        assert len([a for a in awards if a.cancelled_at is None]) == 1

    def test_the_cancelled_bid_stops_being_accepted(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        """Otherwise the cleaner is locked out of re-bidding on a job they can
        now actually make — an accepted bid is one they are not allowed to edit."""
        job = _award_a_job(client, make_cleaner, make_open_turnover)
        client.post(
            f"/api/board/jobs/{job['turnover']['id']}/cancel",
            json={"reason": "Sorry."},
            headers=job["cleaner"]["auth"],
        )

        bid = db.get(Bid, uuid.UUID(job["bid"]["id"]))
        db.refresh(bid)
        assert bid.status is BidStatus.WITHDRAWN

        again = client.put(
            f"/api/board/{job['turnover']['id']}/bid",
            json={"price_cents": 12_500},
            headers=job["cleaner"]["auth"],
        )
        assert again.status_code == 200

    def test_the_gate_code_goes_away_with_the_booking(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        """Access follows the award, not the history."""
        job = _award_a_job(client, make_cleaner, make_open_turnover)

        held = client.get(
            f"/api/board/jobs/{job['turnover']['id']}", headers=job["cleaner"]["auth"]
        ).json()
        assert "4417" in held["property"]["access_notes"]
        assert held["property"]["address_line1"] == "1 Harbor Way"

        client.post(
            f"/api/board/jobs/{job['turnover']['id']}/cancel",
            json={"reason": "Cannot make it."},
            headers=job["cleaner"]["auth"],
        )

        after = client.get(
            f"/api/board/jobs/{job['turnover']['id']}", headers=job["cleaner"]["auth"]
        )
        assert after.status_code == 404, "a cancelled booking is not a live job"

        finished = client.get(
            "/api/board/jobs?include_finished=true", headers=job["cleaner"]["auth"]
        ).json()
        assert len(finished) == 1
        assert finished[0]["property"]["access_notes"] is None
        assert "4417" not in repr(finished)

    def test_someone_elses_job_answers_404(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        job = _award_a_job(client, make_cleaner, make_open_turnover)
        stranger = make_cleaner(cleared=True)

        resp = client.post(
            f"/api/board/jobs/{job['turnover']['id']}/cancel",
            json={"reason": "Not mine, but let us see."},
            headers=stranger["auth"],
        )
        assert resp.status_code == 404
        assert (
            client.get(
                f"/api/board/jobs/{job['turnover']['id']}", headers=stranger["auth"]
            ).status_code
            == 404
        )


class TestANoShow:
    def test_the_booking_ends_flagged_and_the_job_reopens(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        job = _award_a_job(client, make_cleaner, make_open_turnover)

        resp = client.post(
            f"/api/turnovers/{job['turnover']['id']}/no-show",
            json={"reason": "Nobody came, no message."},
            headers=job["owner"]["auth"],
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "open"
        assert resp.json()["award"] is None, "a cancelled booking is not the live one"

        award = db.execute(
            select(Award).where(Award.turnover_id == uuid.UUID(job["turnover"]["id"]))
        ).scalar_one()
        db.refresh(award)
        assert award.was_no_show is True
        assert award.cancelled_at is not None
        assert award.cancellation_reason == "Nobody came, no message."

        assert _notified(db, "cleaner_no_show"), "nobody was told about the no-show"

    def test_a_no_show_needs_a_reason(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        job = _award_a_job(client, make_cleaner, make_open_turnover)
        resp = client.post(
            f"/api/turnovers/{job['turnover']['id']}/no-show",
            json={},
            headers=job["owner"]["auth"],
        )
        assert resp.status_code == 422

    def test_a_turnover_with_nobody_booked_cannot_be_a_no_show(
        self, client: TestClient, make_open_turnover
    ) -> None:
        job = make_open_turnover()
        resp = client.post(
            f"/api/turnovers/{job['turnover']['id']}/no-show",
            json={"reason": "Nobody came."},
            headers=job["owner"]["auth"],
        )
        assert resp.status_code == 409
        assert "nobody booked" in resp.json()["detail"]


class TestAnOwnerCallsItOff:
    def test_cancelling_an_awarded_turnover_ends_the_booking_and_tells_the_cleaner(
        self, client: TestClient, make_cleaner, make_open_turnover, admin_user, db: Session
    ) -> None:
        """Phase 2 refused this outright and pointed at the phase that would
        build it. This is that phase: allowed, but never silently."""
        job = _award_a_job(client, make_cleaner, make_open_turnover)

        resp = client.post(
            f"/api/turnovers/{job['turnover']['id']}/cancel",
            json={"reason": "The guest cancelled their stay."},
            headers=job["owner"]["auth"],
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "cancelled", "the owner does not want it cleaned"
        assert body["award"] is None
        assert body["cancellation_reason"] == "The guest cancelled their stay."

        told = _recipients(db, "owner_cancelled_awarded")
        assert job["cleaner"]["user"]["email"] in told, "the cleaner was not told"
        assert admin_user["user"].email in told
        assert "nobody is booked for it" in _notified(db, "owner_cancelled_awarded")[0].body

    def test_cancelling_on_a_booked_cleaner_requires_a_reason(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        job = _award_a_job(client, make_cleaner, make_open_turnover)

        resp = client.post(
            f"/api/turnovers/{job['turnover']['id']}/cancel",
            json={},
            headers=job["owner"]["auth"],
        )
        assert resp.status_code == 422
        assert "Give a reason" in resp.json()["detail"]

        turnover = db.get(Turnover, uuid.UUID(job["turnover"]["id"]))
        db.refresh(turnover)
        assert turnover.status is TurnoverStatus.AWARDED, "nothing happened"

    def test_an_unstaffed_turnover_still_cancels_without_ceremony(
        self, client: TestClient, make_open_turnover
    ) -> None:
        """Nobody has arranged their day around this one."""
        job = make_open_turnover()
        resp = client.post(
            f"/api/turnovers/{job['turnover']['id']}/cancel",
            json={},
            headers=job["owner"]["auth"],
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"


class TestUrgencyAfterAReopen:
    def test_a_reopened_job_far_out_is_still_judged_on_its_window(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        """`reopened_at` raises urgency where it should and nowhere else.

        Three weeks out with a four-day window, a cancellation is not an
        emergency — and a ladder that cried urgent for every re-post would stop
        meaning anything.
        """
        job = _award_a_job(
            client, make_cleaner, make_open_turnover, days_out=21, checkin_hours_after=96
        )
        client.post(
            f"/api/board/jobs/{job['turnover']['id']}/cancel",
            json={"reason": "Too far out to commit."},
            headers=job["cleaner"]["auth"],
        )

        after = client.get(
            f"/api/turnovers/{job['turnover']['id']}", headers=job["owner"]["auth"]
        ).json()
        assert after["urgency"] == TurnoverUrgency.STANDARD.value
        assert after["reopened_at"] is not None
