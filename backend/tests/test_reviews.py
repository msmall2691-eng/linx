"""Mutual delayed-reveal reviews — and the carry-over row about the reveal.

The carry-over list in CLAUDE.md has had this written down since day one:

    Mutual review reveal — a one-sided review never shows before the other side
    submits or the timeout passes.

It is here, and it is written to fail against the wrong implementation rather
than merely to pass against the right one. The wrong implementation is not
exotic — it is the obvious one, where a review is visible as soon as it is
written. So the tests assert on what the *other side* can see, on what the API
returns, and on the absence of any signal that a review has been written at all:

    Knowing the other side has reviewed you is knowing to hurry. Knowing they
    have not is knowing you can safely go first with a bad one. The response
    shape must carry neither, which is why several tests here read the whole
    JSON body rather than one field.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import NotificationEvent, Turnover, TurnoverStatus
from app.models.review import Review
from app.services import notifications, reviews
from app.tasks import scheduled


def _bid(client: TestClient, cleaner: dict, turnover_id: str, cents: int = 15_000) -> dict:
    resp = client.put(
        f"/api/board/{turnover_id}/bid",
        json={"price_cents": cents, "message": "On it."},
        headers=cleaner["auth"],
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _finished_job(client: TestClient, make_cleaner, make_open_turnover) -> dict:
    """Bid → award → complete. The state a review hangs off."""
    job = make_open_turnover()
    cleaner = make_cleaner(cleared=True)
    bid = _bid(client, cleaner, job["turnover"]["id"])

    accepted = client.post(
        f"/api/turnovers/{job['turnover']['id']}/bids/{bid['id']}/accept",
        headers=job["owner"]["auth"],
    )
    assert accepted.status_code == 200, accepted.text

    done = client.post(
        f"/api/board/jobs/{job['turnover']['id']}/complete", headers=cleaner["auth"]
    )
    assert done.status_code == 200, done.text

    job["cleaner"] = cleaner
    return job


def _write(client: TestClient, who: dict, turnover_id: str, rating: int, text: str | None = None):
    return client.post(
        f"/api/turnovers/{turnover_id}/reviews",
        json={"rating": rating, "text": text},
        headers=who["auth"],
    )


def _read(client: TestClient, who: dict, turnover_id: str) -> dict:
    resp = client.get(f"/api/turnovers/{turnover_id}/reviews", headers=who["auth"])
    assert resp.status_code == 200, resp.text
    return resp.json()


# --------------------------------------------------------------------------
# The carry-over row
# --------------------------------------------------------------------------


class TestTheDelayedReveal:
    def test_one_sided_review_is_invisible_to_the_other_side(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        """**The carry-over test.** Nothing shows until both are in.

        The failure this prevents: a review visible the moment it lands rewards
        getting in first with a bad one to poison what the other side would have
        said. Both people then write defensively and the ratings stop meaning
        anything.
        """
        job = _finished_job(client, make_cleaner, make_open_turnover)
        assert _write(client, job["owner"], job["turnover"]["id"], 2, "Sloppy.").status_code == 201

        seen = _read(client, job["cleaner"], job["turnover"]["id"])
        assert seen["visible"] == [], "the owner's review leaked before the cleaner wrote"
        assert seen["mine"] is None
        assert seen["can_review"] is True

        row = db.execute(select(Review)).scalars().one()
        assert row.visible_at is None, "a lone review was revealed"

    def test_the_response_does_not_reveal_that_they_wrote_one(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        """Not just the text — the *fact* of it.

        Knowing the other side has already reviewed you is knowing to hurry.
        Knowing they have not is knowing you can safely go first with a bad one.
        So the body is read whole rather than field by field: no count, no
        "awaiting", no timestamp to difference.
        """
        job = _finished_job(client, make_cleaner, make_open_turnover)
        before = json.dumps(_read(client, job["cleaner"], job["turnover"]["id"]))

        _write(client, job["owner"], job["turnover"]["id"], 1, "Terrible.")
        after = json.dumps(_read(client, job["cleaner"], job["turnover"]["id"]))

        assert before == after, (
            "the cleaner's view changed when the owner reviewed — the fact of a "
            "review having been written is itself the signal the delay withholds"
        )

    def test_the_second_review_reveals_both_at_once(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        """Both, together. Revealing one first is the thing the delay prevents."""
        job = _finished_job(client, make_cleaner, make_open_turnover)
        _write(client, job["owner"], job["turnover"]["id"], 5, "Spotless.")
        second = _write(client, job["cleaner"], job["turnover"]["id"], 4, "Easy job.")
        assert second.status_code == 201, second.text

        rows = db.execute(select(Review)).scalars().all()
        assert len(rows) == 2
        assert all(row.visible_at is not None for row in rows), "one stayed hidden"
        assert len({row.visible_at for row in rows}) == 1, "revealed at different moments"

        for side in (job["owner"], job["cleaner"]):
            seen = _read(client, side, job["turnover"]["id"])
            assert seen["mine"] is not None
            assert len(seen["visible"]) == 1, "each side should see the other's"

    def test_a_review_nobody_answers_is_revealed_once_the_window_passes(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        """Silence is not a veto.

        If an unanswered review stayed hidden forever, refusing to write one
        would be the way to bury a bad review — the same suppression the delay
        exists to prevent, achieved by doing nothing instead.
        """
        job = _finished_job(client, make_cleaner, make_open_turnover)
        _write(client, job["owner"], job["turnover"]["id"], 2, "Not great.")

        row = db.execute(select(Review)).scalars().one()
        assert row.visible_at is None

        later = datetime.now(timezone.utc) + reviews.reveal_window() + timedelta(minutes=1)
        assert reviews.reveal_overdue(db, now=later) == 1

        db.expire_all()
        assert db.execute(select(Review)).scalars().one().visible_at is not None
        seen = _read(client, job["cleaner"], job["turnover"]["id"])
        assert len(seen["visible"]) == 1

    def test_it_is_not_revealed_one_minute_early(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        """The window is a real boundary, not a suggestion."""
        job = _finished_job(client, make_cleaner, make_open_turnover)
        _write(client, job["owner"], job["turnover"]["id"], 3)

        nearly = datetime.now(timezone.utc) + reviews.reveal_window() - timedelta(minutes=1)
        assert reviews.reveal_overdue(db, now=nearly) == 0
        assert db.execute(select(Review)).scalars().one().visible_at is None

    def test_the_scheduled_pass_is_what_gives_it_a_clock(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        """The reveal is the one scheduled job that is load-bearing.

        If it stops running, one-sided reviews stay hidden forever and silence
        becomes a veto — quietly, with nothing failing.
        """
        job = _finished_job(client, make_cleaner, make_open_turnover)
        _write(client, job["owner"], job["turnover"]["id"], 4)

        later = datetime.now(timezone.utc) + reviews.reveal_window() + timedelta(hours=1)
        assert scheduled.run(db, now=later)["revealed"] == 1
        assert db.execute(select(Review)).scalars().one().visible_at is not None

    def test_a_second_sweep_does_not_reveal_it_again(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        """The job runs often. Revealing twice would notify twice."""
        job = _finished_job(client, make_cleaner, make_open_turnover)
        _write(client, job["owner"], job["turnover"]["id"], 4)

        later = datetime.now(timezone.utc) + reviews.reveal_window() + timedelta(hours=1)
        assert reviews.reveal_overdue(db, now=later) == 1
        assert reviews.reveal_overdue(db, now=later) == 0

        rows = db.execute(
            select(notifications.Notification).where(
                notifications.Notification.event == NotificationEvent.REVIEW_RECEIVED
            )
        ).scalars().all()
        assert len(rows) == 1

    def test_visible_at_has_exactly_one_author(self) -> None:
        """Grep-level, and deliberately so.

        A second place that writes `visible_at` is a second opinion about the
        rule this whole phase is, and it would not fail any other test here
        until it disagreed in production.
        """
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1] / "app"
        writers = [
            path
            for path in root.rglob("*.py")
            if "visible_at =" in path.read_text()
        ]
        assert [p.name for p in writers] == ["reviews.py"], (
            f"visible_at is written outside the service: {[str(p) for p in writers]}"
        )


# --------------------------------------------------------------------------
# Who may write one, and when
# --------------------------------------------------------------------------


class TestWhoCanReview:
    def test_both_sides_of_a_finished_job_can(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        job = _finished_job(client, make_cleaner, make_open_turnover)
        for side in (job["owner"], job["cleaner"]):
            assert _read(client, side, job["turnover"]["id"])["can_review"] is True

    def test_an_unfinished_job_has_nothing_to_review(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        """Reviews are for work that happened.

        A turnover whose checkout time has passed is not evidence anybody
        cleaned anything — the same reason money hangs off completion.
        """
        job = make_open_turnover()
        cleaner = make_cleaner(cleared=True)
        bid = _bid(client, cleaner, job["turnover"]["id"])
        client.post(
            f"/api/turnovers/{job['turnover']['id']}/bids/{bid['id']}/accept",
            headers=job["owner"]["auth"],
        )

        resp = _write(client, job["owner"], job["turnover"]["id"], 5)
        assert resp.status_code == 404

    def test_a_cancelled_booking_is_a_dispute_not_a_review(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        """Deliberately out of scope, and worth saying why.

        There is no mutual review to be had when one side did not turn up, so
        the delayed reveal has nothing to balance. The product records it where
        it belongs instead: `was_no_show` on the award, an admin alerted, and a
        human answering the dispute.
        """
        job = make_open_turnover()
        cleaner = make_cleaner(cleared=True)
        bid = _bid(client, cleaner, job["turnover"]["id"])
        client.post(
            f"/api/turnovers/{job['turnover']['id']}/bids/{bid['id']}/accept",
            headers=job["owner"]["auth"],
        )
        client.post(
            f"/api/board/jobs/{job['turnover']['id']}/cancel",
            json={"reason": "Van broke down."},
            headers=cleaner["auth"],
        )

        assert _write(client, job["owner"], job["turnover"]["id"], 1).status_code == 404

    def test_a_stranger_cannot_review_or_read(
        self, client: TestClient, make_cleaner, make_open_turnover, make_user
    ) -> None:
        """404, not 403 — a 403 would confirm the turnover exists."""
        job = _finished_job(client, make_cleaner, make_open_turnover)
        stranger = make_user(role="owner")

        assert _write(client, stranger, job["turnover"]["id"], 5).status_code == 404
        assert (
            client.get(
                f"/api/turnovers/{job['turnover']['id']}/reviews", headers=stranger["auth"]
            ).status_code
            == 404
        )

    def test_nobody_reviews_twice(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        """No edits either.

        A review you can rewrite after the other side's appears is a review you
        can rewrite *in response to* it, which is the behaviour the delay exists
        to prevent.
        """
        job = _finished_job(client, make_cleaner, make_open_turnover)
        assert _write(client, job["owner"], job["turnover"]["id"], 5).status_code == 201

        again = _write(client, job["owner"], job["turnover"]["id"], 1, "Changed my mind.")
        assert again.status_code == 409
        assert "already reviewed" in again.json()["detail"]

    def test_you_can_always_read_your_own(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        """Showing you what you wrote tells you nothing you did not know."""
        job = _finished_job(client, make_cleaner, make_open_turnover)
        _write(client, job["owner"], job["turnover"]["id"], 3, "Fine.")

        seen = _read(client, job["owner"], job["turnover"]["id"])
        assert seen["mine"]["rating"] == 3
        assert seen["mine"]["text"] == "Fine."
        assert seen["mine"]["visible_at"] is None
        assert seen["visible"] == [], "own review must not double as a visible one"

    @pytest.mark.parametrize("rating", [0, 6, -1])
    def test_a_rating_outside_one_to_five_is_refused(
        self, client: TestClient, make_cleaner, make_open_turnover, rating: int
    ) -> None:
        job = _finished_job(client, make_cleaner, make_open_turnover)
        assert _write(client, job["owner"], job["turnover"]["id"], rating).status_code == 422

    def test_words_are_optional(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        """A star with no words is still a review; forcing prose produces filler."""
        job = _finished_job(client, make_cleaner, make_open_turnover)
        resp = _write(client, job["owner"], job["turnover"]["id"], 5)
        assert resp.status_code == 201
        assert resp.json()["mine"]["text"] is None


# --------------------------------------------------------------------------
# The notification — the last entry on the fixed list
# --------------------------------------------------------------------------


class TestTheNotification:
    def test_nobody_is_told_when_a_review_is_merely_written(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        """**The whole reason this event fires on reveal rather than on write.**

        "You have a new review" landing the moment the other side submits hands
        them exactly what the delay withholds: that a review exists, and by
        implication how soon they need to get theirs in.
        """
        job = _finished_job(client, make_cleaner, make_open_turnover)
        _write(client, job["owner"], job["turnover"]["id"], 2, "Disappointing.")

        assert not db.execute(
            select(notifications.Notification).where(
                notifications.Notification.event == NotificationEvent.REVIEW_RECEIVED
            )
        ).scalars().all(), "the cleaner was told a review existed before it was visible"

    def test_both_are_told_when_both_are_revealed(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        job = _finished_job(client, make_cleaner, make_open_turnover)
        _write(client, job["owner"], job["turnover"]["id"], 5, "Great.")
        _write(client, job["cleaner"], job["turnover"]["id"], 5, "Lovely place.")

        told = {
            row.destination
            for row in db.execute(
                select(notifications.Notification).where(
                    notifications.Notification.event == NotificationEvent.REVIEW_RECEIVED
                )
            ).scalars()
        }
        assert told == {
            job["owner"]["user"]["email"],
            job["cleaner"]["user"]["email"],
        }

    def test_the_person_told_is_the_one_reviewed_not_the_author(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        """An author already knows what they wrote."""
        job = _finished_job(client, make_cleaner, make_open_turnover)
        _write(client, job["owner"], job["turnover"]["id"], 2, "Missed the oven.")

        later = datetime.now(timezone.utc) + reviews.reveal_window() + timedelta(hours=1)
        reviews.reveal_overdue(db, now=later)

        rows = db.execute(
            select(notifications.Notification).where(
                notifications.Notification.event == NotificationEvent.REVIEW_RECEIVED
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].destination == job["cleaner"]["user"]["email"], (
            "the owner was told about their own review"
        )
        assert "Missed the oven." in rows[0].body

    def test_the_whole_fixed_list_now_has_a_sender(self) -> None:
        """Thirteen events, thirteen senders. Nothing left declared-and-unwired.

        The counterpart of the test that has guarded this list since phase 5 —
        which asserted the opposite, that `review_received` had no sender. That
        assertion coming out is how a phase is finished rather than forgotten.
        """
        senders = {
            NotificationEvent.TURNOVER_POSTED: notifications.turnover_posted,
            NotificationEvent.BID_RECEIVED: notifications.bid_received,
            NotificationEvent.BID_ACCEPTED: notifications.bid_accepted,
            NotificationEvent.BID_DECLINED: notifications.bids_declined,
            NotificationEvent.TURNOVER_REMINDER: notifications.turnover_reminder,
            NotificationEvent.CLEANER_CANCELLED: notifications.award_cancelled,
            NotificationEvent.CLEANER_NO_SHOW: notifications.award_cancelled,
            NotificationEvent.OWNER_CANCELLED_AWARDED: notifications.award_cancelled,
            NotificationEvent.TURNOVER_UNCLAIMED: notifications.turnover_unclaimed,
            NotificationEvent.JOB_COMPLETED: notifications.job_completed,
            NotificationEvent.PAYMENT_RECEIPT: notifications.payment_receipt,
            NotificationEvent.PAYOUT_NOTICE: notifications.payout_notice,
            NotificationEvent.REVIEW_RECEIVED: notifications.review_received,
        }
        assert set(senders) == set(NotificationEvent)


# --------------------------------------------------------------------------
# Reputation — shown, never ranked
# --------------------------------------------------------------------------


class TestReputation:
    def test_it_counts_only_what_is_visible(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        """A rating built from hidden reviews leaks them by arithmetic."""
        job = _finished_job(client, make_cleaner, make_open_turnover)
        _write(client, job["owner"], job["turnover"]["id"], 1, "Bad.")

        cleaner_id = uuid.UUID(job["cleaner"]["user"]["id"])
        assert reviews.reputation_of(db, cleaner_id) == reviews.Reputation(
            count=0, average=None
        )

        _write(client, job["cleaner"], job["turnover"]["id"], 5)
        db.expire_all()
        assert reviews.reputation_of(db, cleaner_id) == reviews.Reputation(
            count=1, average=1.0
        )

    def test_a_cleaners_rating_is_what_owners_said_about_them(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        """Keyed on the subject, not the author — a cleaner's own generous
        reviews of owners must not inflate their own score."""
        job = _finished_job(client, make_cleaner, make_open_turnover)
        _write(client, job["owner"], job["turnover"]["id"], 3)
        _write(client, job["cleaner"], job["turnover"]["id"], 5)
        db.expire_all()

        cleaner_id = uuid.UUID(job["cleaner"]["user"]["id"])
        owner_id = uuid.UUID(job["owner"]["user"]["id"])
        assert reviews.reputation_of(db, cleaner_id).average == 3.0
        assert reviews.reputation_of(db, owner_id).average == 5.0

    def test_somebody_with_no_reviews_has_none_rather_than_zero(
        self, client: TestClient, make_cleaner, db: Session
    ) -> None:
        """Null, not 0.0. A new cleaner is unrated, not badly rated — and the
        board still sorts by urgency, so they are not buried either."""
        cleaner = make_cleaner(cleared=True)
        reputation = reviews.reputation_of(db, uuid.UUID(cleaner["user"]["id"]))
        assert reputation.count == 0
        assert reputation.average is None

    def test_the_endpoint_answers_for_any_signed_in_user(
        self, client: TestClient, make_cleaner, make_open_turnover, make_user
    ) -> None:
        """A rating is the thing a marketplace exists to make public."""
        job = _finished_job(client, make_cleaner, make_open_turnover)
        _write(client, job["owner"], job["turnover"]["id"], 4)
        _write(client, job["cleaner"], job["turnover"]["id"], 4)

        stranger = make_user(role="owner")
        resp = client.get(
            f"/api/cleaners/{job['cleaner']['user']['id']}/reputation",
            headers=stranger["auth"],
        )
        assert resp.status_code == 200
        assert resp.json() == {"count": 1, "average": 4.0}

    def test_the_batch_lookup_agrees_with_the_single_one(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        """Two definitions of the same number is one definition too many."""
        job = _finished_job(client, make_cleaner, make_open_turnover)
        _write(client, job["owner"], job["turnover"]["id"], 2)
        _write(client, job["cleaner"], job["turnover"]["id"], 5)
        db.expire_all()

        cleaner_id = uuid.UUID(job["cleaner"]["user"]["id"])
        assert reviews.reputation_counts(db, [cleaner_id])[cleaner_id] == (
            reviews.reputation_of(db, cleaner_id)
        )

    def test_the_average_is_rounded_once_and_never_drifts(
        self, client: TestClient, make_cleaner, make_open_turnover, make_user, db: Session
    ) -> None:
        """One division, rounded once, in the one place that computes it."""
        owner = make_user(role="owner")
        cleaner = make_cleaner(cleared=True)

        for rating in (4, 5, 5):
            job = make_open_turnover(owner=owner)
            bid = _bid(client, cleaner, job["turnover"]["id"])
            client.post(
                f"/api/turnovers/{job['turnover']['id']}/bids/{bid['id']}/accept",
                headers=owner["auth"],
            )
            client.post(
                f"/api/board/jobs/{job['turnover']['id']}/complete", headers=cleaner["auth"]
            )
            _write(client, owner, job["turnover"]["id"], rating)
            _write(client, cleaner, job["turnover"]["id"], 5)

        db.expire_all()
        cleaner_id = uuid.UUID(cleaner["user"]["id"])
        # 14 / 3 = 4.666…
        assert reviews.reputation_of(db, cleaner_id) == reviews.Reputation(
            count=3, average=4.7
        )


# --------------------------------------------------------------------------
# The window itself
# --------------------------------------------------------------------------


class TestTheWindow:
    def test_it_comes_from_settings_rather_than_a_literal(self, monkeypatch) -> None:
        """One number, in one place, so the policy and the sweep agree."""
        monkeypatch.setattr(settings, "review_reveal_after_days", 3)
        assert reviews.reveal_window() == timedelta(days=3)

    def test_shortening_it_reveals_sooner(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session, monkeypatch
    ) -> None:
        job = _finished_job(client, make_cleaner, make_open_turnover)
        _write(client, job["owner"], job["turnover"]["id"], 4)

        monkeypatch.setattr(settings, "review_reveal_after_days", 1)
        later = datetime.now(timezone.utc) + timedelta(days=1, minutes=1)
        assert reviews.reveal_overdue(db, now=later) == 1


class TestQueueingStaysInTheTransaction:
    def test_a_reveal_and_its_notification_land_together(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        """`reveal_pair` does not commit, so "it became visible" and "they were
        told" cannot come apart."""
        job = _finished_job(client, make_cleaner, make_open_turnover)
        _write(client, job["owner"], job["turnover"]["id"], 4)

        turnover = db.get(Turnover, uuid.UUID(job["turnover"]["id"]))
        assert turnover.status is TurnoverStatus.COMPLETED

        review = db.execute(select(Review)).scalars().one()
        reviews.reveal_pair(db, turnover, [review])
        db.rollback()

        db.expire_all()
        assert db.execute(select(Review)).scalars().one().visible_at is None
        assert not db.execute(
            select(notifications.Notification).where(
                notifications.Notification.event == NotificationEvent.REVIEW_RECEIVED
            )
        ).scalars().all()
