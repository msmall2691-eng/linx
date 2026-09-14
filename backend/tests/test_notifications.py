"""Notifications — the fixed event list, and the rule that makes it complete.

The completeness rule from CLAUDE.md is that *a state transition is not done
until its row in the event list has both a sender and a test asserting it
fires*. Not a test that the code path exists — a test that fails if the send is
deleted. Deleting a send is silent in every other way.

So every event with a live trigger gets one here, and the three that do not
(payment receipt, payout notice, review received) are asserted to be declared
and unwired, so that phases 6 and 7 inherit a list rather than a memory.

The other half is duplicates, which are as bad as misses: an alert that arrives
every fifteen minutes gets muted, and a muted channel sends nothing. That is
what `dedupe_key` is for, and it is tested directly rather than assumed.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Notification,
    NotificationEvent,
    NotificationStatus,
    Turnover,
    TurnoverStatus,
)
from app.db import SessionLocal
from app.services import delivery, notifications
from app.tasks import scheduled


def _rows(db: Session, event: NotificationEvent) -> list[Notification]:
    db.expire_all()
    return list(
        db.execute(
            select(Notification)
            .where(Notification.event == event)
            .order_by(Notification.created_at)
        )
        .scalars()
        .all()
    )


def _to(db: Session, event: NotificationEvent) -> set[str]:
    return {row.destination for row in _rows(db, event)}


def _bid(client: TestClient, cleaner: dict, turnover_id: str, cents: int = 12_000) -> dict:
    resp = client.put(
        f"/api/board/{turnover_id}/bid",
        json={"price_cents": cents},
        headers=cleaner["auth"],
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestTheEventsThatHaveTriggers:
    """One test per row of the fixed list that something can fire today."""

    def test_a_posted_turnover_reaches_cleaners_in_range(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        near = make_cleaner(cleared=True)
        make_open_turnover()

        told = _to(db, NotificationEvent.TURNOVER_POSTED)
        assert near["user"]["email"] in told

    def test_it_does_not_reach_a_cleaner_out_of_range(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        """Boston is ~98 miles from Portland; the radius is 25."""
        far = make_cleaner(cleared=True, lat=42.3601, lng=-71.0589)
        make_open_turnover()

        assert far["user"]["email"] not in _to(db, NotificationEvent.TURNOVER_POSTED)

    def test_it_does_not_reach_a_cleaner_who_cannot_bid_on_it(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        """Telling somebody about work they are not cleared to take is noise,
        and noise is how a channel gets muted."""
        unvetted = make_cleaner(cleared=False)
        make_open_turnover()

        assert unvetted["user"]["email"] not in _to(db, NotificationEvent.TURNOVER_POSTED)

    def test_publishing_a_draft_counts_as_posting_it(
        self, client: TestClient, make_cleaner, make_user, db: Session
    ) -> None:
        cleaner = make_cleaner(cleared=True)
        owner = make_user(role="owner")
        prop = client.post(
            "/api/properties",
            json={
                "nickname": "Draft House",
                "address_line1": "2 Harbor Way",
                "city": "Portland",
                "state": "ME",
                "postal_code": "04101",
                "lat": "43.6591",
                "lng": "-70.2568",
                "bedrooms": 1,
                "bathrooms": "1",
            },
            headers=owner["auth"],
        ).json()
        created = client.post(
            "/api/turnovers",
            json={
                "property_id": prop["id"],
                "checkout_at": (
                    datetime.now(timezone.utc) + timedelta(days=5)
                ).isoformat(),
                "publish": False,
            },
            headers=owner["auth"],
        ).json()

        assert not _rows(db, NotificationEvent.TURNOVER_POSTED), "a draft is not posted"

        client.post(f"/api/turnovers/{created['id']}/publish", headers=owner["auth"])
        assert cleaner["user"]["email"] in _to(db, NotificationEvent.TURNOVER_POSTED)

    def test_a_job_that_comes_back_on_the_bench_is_posted_again(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        """The re-post half of the cancellation policy, as a notification.

        Flipping the status back to OPEN puts the job on the board; it does not
        put it in front of anybody. A cleaner who was told when it was first
        posted — and who did not take it — must hear about it again, because the
        turnover-id-only dedupe key would otherwise swallow the one posting that
        cannot wait for somebody to refresh a page.
        """
        winner = make_cleaner(cleared=True)
        bystander = make_cleaner(cleared=True)
        job = make_open_turnover()

        # The bystander is told when it is first posted, and does not bid.
        assert bystander["user"]["email"] in _to(db, NotificationEvent.TURNOVER_POSTED)
        first_time = len(_rows(db, NotificationEvent.TURNOVER_POSTED))

        bid = _bid(client, winner, job["turnover"]["id"])
        client.post(
            f"/api/turnovers/{job['turnover']['id']}/bids/{bid['id']}/accept",
            headers=job["owner"]["auth"],
        )
        # And then the cleaner backs out.
        client.post(
            f"/api/board/jobs/{job['turnover']['id']}/cancel",
            json={"reason": "Cannot make it after all."},
            headers=winner["auth"],
        )

        rows = _rows(db, NotificationEvent.TURNOVER_POSTED)
        assert len(rows) > first_time, "the re-post reached nobody"
        assert bystander["user"]["email"] in _to(db, NotificationEvent.TURNOVER_POSTED)
        assert len({row.dedupe_key for row in rows}) == len(rows)

    def test_a_bid_tells_the_owner(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        job = make_open_turnover()
        cleaner = make_cleaner(cleared=True)
        _bid(client, cleaner, job["turnover"]["id"], 13_500)

        rows = _rows(db, NotificationEvent.BID_RECEIVED)
        assert [row.destination for row in rows] == [job["owner"]["user"]["email"]]
        assert "$135.00" in rows[0].subject

    def test_the_same_price_twice_does_not_tell_them_twice(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        job = make_open_turnover()
        cleaner = make_cleaner(cleared=True)
        _bid(client, cleaner, job["turnover"]["id"], 13_500)
        _bid(client, cleaner, job["turnover"]["id"], 13_500)

        assert len(_rows(db, NotificationEvent.BID_RECEIVED)) == 1

    def test_but_a_changed_price_does(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        """A new number is new information; the dedupe key includes the price."""
        job = make_open_turnover()
        cleaner = make_cleaner(cleared=True)
        _bid(client, cleaner, job["turnover"]["id"], 13_500)
        _bid(client, cleaner, job["turnover"]["id"], 11_000)

        assert len(_rows(db, NotificationEvent.BID_RECEIVED)) == 2

    def test_accepting_tells_the_winner_and_every_loser(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        job = make_open_turnover()
        winner, loser = make_cleaner(cleared=True), make_cleaner(cleared=True)
        winning = _bid(client, winner, job["turnover"]["id"], 11_000)
        _bid(client, loser, job["turnover"]["id"], 15_000)

        client.post(
            f"/api/turnovers/{job['turnover']['id']}/bids/{winning['id']}/accept",
            headers=job["owner"]["auth"],
        )

        assert _to(db, NotificationEvent.BID_ACCEPTED) == {winner["user"]["email"]}
        assert _to(db, NotificationEvent.BID_DECLINED) == {loser["user"]["email"]}

    def test_declining_by_hand_tells_that_cleaner(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        job = make_open_turnover()
        cleaner = make_cleaner(cleared=True)
        bid = _bid(client, cleaner, job["turnover"]["id"])

        client.post(
            f"/api/turnovers/{job['turnover']['id']}/bids/{bid['id']}/decline",
            headers=job["owner"]["auth"],
        )
        assert _to(db, NotificationEvent.BID_DECLINED) == {cleaner["user"]["email"]}


class TestTheScheduledEvents:
    """The two nothing triggers: the day-of reminder, and the alarm."""

    def _award(self, client: TestClient, make_cleaner, make_open_turnover, **kw) -> dict:
        job = make_open_turnover(**kw)
        cleaner = make_cleaner(cleared=True)
        bid = _bid(client, cleaner, job["turnover"]["id"])
        accepted = client.post(
            f"/api/turnovers/{job['turnover']['id']}/bids/{bid['id']}/accept",
            headers=job["owner"]["auth"],
        )
        assert accepted.status_code == 200, accepted.text
        job["cleaner"] = cleaner
        return job

    def test_a_booked_turnover_tomorrow_reminds_both_sides(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        job = self._award(client, make_cleaner, make_open_turnover, days_out=1)

        scheduled.send_reminders(db)

        told = _to(db, NotificationEvent.TURNOVER_REMINDER)
        assert job["owner"]["user"]["email"] in told
        assert job["cleaner"]["user"]["email"] in told

    def test_a_booking_weeks_away_is_not_reminded_yet(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        self._award(client, make_cleaner, make_open_turnover, days_out=20)
        scheduled.send_reminders(db)
        assert not _rows(db, NotificationEvent.TURNOVER_REMINDER)

    def test_running_the_job_again_does_not_remind_again(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        """The point of the dedupe key: a five-minute cron, one reminder."""
        self._award(client, make_cleaner, make_open_turnover, days_out=1)

        scheduled.send_reminders(db)
        first = len(_rows(db, NotificationEvent.TURNOVER_REMINDER))
        for _ in range(3):
            scheduled.send_reminders(db)

        assert len(_rows(db, NotificationEvent.TURNOVER_REMINDER)) == first
        assert first > 0

    def test_an_unclaimed_turnover_close_to_checkout_alerts_owner_and_admin(
        self, client: TestClient, make_open_turnover, admin_user, db: Session
    ) -> None:
        job = make_open_turnover(days_out=0, checkin_hours_after=None)

        scheduled.alert_unclaimed(db)

        told = _to(db, NotificationEvent.TURNOVER_UNCLAIMED)
        assert job["owner"]["user"]["email"] in told, "the owner was not told"
        assert admin_user["user"].email in told, "no admin was told"

    def test_a_turnover_somebody_took_is_not_unclaimed(
        self, client: TestClient, make_cleaner, make_open_turnover, admin_user, db: Session
    ) -> None:
        self._award(client, make_cleaner, make_open_turnover, days_out=0)

        scheduled.alert_unclaimed(db)
        assert not _rows(db, NotificationEvent.TURNOVER_UNCLAIMED)

    def test_an_unclaimed_turnover_far_out_is_not_an_alarm_yet(
        self, client: TestClient, make_open_turnover, admin_user, db: Session
    ) -> None:
        make_open_turnover(days_out=10)
        scheduled.alert_unclaimed(db)
        assert not _rows(db, NotificationEvent.TURNOVER_UNCLAIMED)


class TestTheOutbox:
    def test_a_queued_notification_is_recorded_before_it_is_sent(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        """Record, commit, then send — so a crash mid-send leaves a row."""
        job = make_open_turnover()
        cleaner = make_cleaner(cleared=True)
        _bid(client, cleaner, job["turnover"]["id"])

        row = _rows(db, NotificationEvent.BID_RECEIVED)[0]
        assert row.attempted_at is not None, "the attempt was not written"
        assert row.subject and row.body, "the message is stored, not re-rendered later"

    def test_without_a_mail_provider_nothing_claims_to_be_sent(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        """The launch posture. A logged notification is not a delivered one, and
        the row must not pretend otherwise."""
        job = make_open_turnover()
        cleaner = make_cleaner(cleared=True)
        _bid(client, cleaner, job["turnover"]["id"])

        row = _rows(db, NotificationEvent.BID_RECEIVED)[0]
        assert row.status is NotificationStatus.PENDING
        assert row.sent_at is None
        assert "no SMTP host" in (row.failure_message or "")

    def test_a_working_sender_marks_it_sent(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session, monkeypatch
    ) -> None:
        sent: list[delivery.Outgoing] = []

        class Working(delivery.Sender):
            delivers = True

            def send(self, message: delivery.Outgoing) -> None:
                sent.append(message)

        monkeypatch.setattr(delivery, "get_sender", lambda: Working())

        job = make_open_turnover()
        cleaner = make_cleaner(cleared=True)
        _bid(client, cleaner, job["turnover"]["id"])

        row = _rows(db, NotificationEvent.BID_RECEIVED)[0]
        assert row.status is NotificationStatus.SENT
        assert row.sent_at is not None
        assert [message.destination for message in sent] == [job["owner"]["user"]["email"]]

    def test_a_failed_send_is_visible_rather_than_swallowed(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session, monkeypatch
    ) -> None:
        class Broken(delivery.Sender):
            def send(self, message: delivery.Outgoing) -> None:
                raise delivery.DeliveryError("mailbox full")

        monkeypatch.setattr(delivery, "get_sender", lambda: Broken())

        job = make_open_turnover()
        cleaner = make_cleaner(cleared=True)
        _bid(client, cleaner, job["turnover"]["id"])

        row = _rows(db, NotificationEvent.BID_RECEIVED)[0]
        assert row.status is NotificationStatus.FAILED
        assert "mailbox full" in (row.failure_message or "")
        assert row.sent_at is None

    def test_one_failure_does_not_stop_the_rest(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session, monkeypatch
    ) -> None:
        class Picky(delivery.Sender):
            def send(self, message: delivery.Outgoing) -> None:
                if "loser" in message.subject or "Not this time" in message.subject:
                    raise delivery.DeliveryError("refused")

        monkeypatch.setattr(delivery, "get_sender", lambda: Picky())

        job = make_open_turnover()
        winner, loser = make_cleaner(cleared=True), make_cleaner(cleared=True)
        winning = _bid(client, winner, job["turnover"]["id"], 11_000)
        _bid(client, loser, job["turnover"]["id"], 15_000)
        client.post(
            f"/api/turnovers/{job['turnover']['id']}/bids/{winning['id']}/accept",
            headers=job["owner"]["auth"],
        )

        assert _rows(db, NotificationEvent.BID_ACCEPTED)[0].status is NotificationStatus.SENT
        assert _rows(db, NotificationEvent.BID_DECLINED)[0].status is NotificationStatus.FAILED

    def test_two_drains_at_once_do_not_send_the_same_thing_twice(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session, monkeypatch
    ) -> None:
        """The dedupe key makes the row unique; it does not make the send once.

        Every request drains the outbox now, so two drains overlapping is the
        ordinary case — an owner accepting a bid while somebody else places one.
        Without the claim both drains select the same pending rows and both send
        them, and the person gets the same email twice.

        Two real connections, because one session cannot race itself: the first
        claims the row and has not sent it yet; the second must find nothing.
        """
        job = make_open_turnover()
        cleaner = make_cleaner(cleared=True)
        _bid(client, cleaner, job["turnover"]["id"])

        row = _rows(db, NotificationEvent.BID_RECEIVED)[0]
        row.status = NotificationStatus.PENDING
        row.attempted_at = None
        row.failure_message = None
        db.commit()

        sent: list[delivery.Outgoing] = []

        class Working(delivery.Sender):
            delivers = True

            def send(self, message: delivery.Outgoing) -> None:
                sent.append(message)

        monkeypatch.setattr(delivery, "get_sender", lambda: Working())

        first, second = SessionLocal(), SessionLocal()
        try:
            claimed = notifications._claim(first, 100)
            assert len(claimed) == 1, "the first drain did not take the row"

            # The second drain arrives while the first is still mid-send.
            assert notifications.deliver_pending(second) == 0
        finally:
            first.close()
            second.close()

        assert sent == [], "the second drain sent a row the first had claimed"

    def test_a_send_that_died_mid_flight_is_not_blindly_resent(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session, monkeypatch
    ) -> None:
        """Guardrail 2's rule, applied to a send.

        An attempted row with no outcome is not assumed failed any more than it
        is assumed successful. Assuming failure is how somebody gets the same
        message twice, and the row is left visibly attempted for a person to
        look at instead.
        """
        job = make_open_turnover()
        cleaner = make_cleaner(cleared=True)
        _bid(client, cleaner, job["turnover"]["id"])

        row = _rows(db, NotificationEvent.BID_RECEIVED)[0]
        row.status = NotificationStatus.PENDING
        row.attempted_at = datetime.now(timezone.utc)
        row.failure_message = None
        db.commit()

        sent: list[delivery.Outgoing] = []

        class Working(delivery.Sender):
            delivers = True

            def send(self, message: delivery.Outgoing) -> None:
                sent.append(message)

        monkeypatch.setattr(delivery, "get_sender", lambda: Working())
        assert notifications.deliver_pending(db) == 0
        assert sent == []

        row = _rows(db, NotificationEvent.BID_RECEIVED)[0]
        assert row.status is NotificationStatus.PENDING, "still visible for review"

    def test_the_scheduled_job_drains_what_a_request_left_behind(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session, monkeypatch
    ) -> None:
        """A process that dies before delivery leaves rows, not a lost message."""
        job = make_open_turnover()
        cleaner = make_cleaner(cleared=True)
        _bid(client, cleaner, job["turnover"]["id"])

        # Put it back the way a crashed request would have left it.
        row = _rows(db, NotificationEvent.BID_RECEIVED)[0]
        row.status = NotificationStatus.PENDING
        row.attempted_at = None
        row.failure_message = None
        db.commit()

        sent: list[delivery.Outgoing] = []

        class Working(delivery.Sender):
            delivers = True

            def send(self, message: delivery.Outgoing) -> None:
                sent.append(message)

        monkeypatch.setattr(delivery, "get_sender", lambda: Working())
        assert scheduled.run(db)["delivered"] == 1
        assert len(sent) == 1


class TestTheListItself:
    def test_every_event_in_claude_md_is_declared(self) -> None:
        """The list is fixed before the feature is built, so it is a closed set.

        Thirteen now, not the original twelve. `job_completed` was added in
        phase 6 with the transition it belongs to, and this test is how that
        addition had to be a decision: it failed the moment the value appeared,
        which is exactly the conversation a new event is supposed to start.
        """
        assert {event.value for event in NotificationEvent} == {
            "turnover_posted",
            "bid_received",
            "bid_accepted",
            "bid_declined",
            "turnover_reminder",
            "cleaner_cancelled",
            "cleaner_no_show",
            "owner_cancelled_awarded",
            "turnover_unclaimed",
            "job_completed",
            "payment_receipt",
            "payout_notice",
            "review_received",
        }

    def test_every_declared_event_now_has_a_sender(self) -> None:
        """Phase 7 emptied this. Nothing is declared-and-unwired any more.

        This test used to assert the opposite — that `review_received` had no
        sender — and that assertion coming out is how a phase gets finished
        rather than forgotten. What it guards now is the other direction: a
        *new* enum value with nothing behind it fails here, which is the
        conversation adding one is supposed to start.
        """
        from app.services import notifications as n

        senders = {
            NotificationEvent.TURNOVER_POSTED: n.turnover_posted,
            NotificationEvent.BID_RECEIVED: n.bid_received,
            NotificationEvent.BID_ACCEPTED: n.bid_accepted,
            NotificationEvent.BID_DECLINED: n.bids_declined,
            NotificationEvent.TURNOVER_REMINDER: n.turnover_reminder,
            NotificationEvent.CLEANER_CANCELLED: n.award_cancelled,
            NotificationEvent.CLEANER_NO_SHOW: n.award_cancelled,
            NotificationEvent.OWNER_CANCELLED_AWARDED: n.award_cancelled,
            NotificationEvent.TURNOVER_UNCLAIMED: n.turnover_unclaimed,
            NotificationEvent.JOB_COMPLETED: n.job_completed,
            NotificationEvent.PAYMENT_RECEIPT: n.payment_receipt,
            NotificationEvent.PAYOUT_NOTICE: n.payout_notice,
            NotificationEvent.REVIEW_RECEIVED: n.review_received,
        }
        assert set(senders) == set(NotificationEvent), (
            "an event is declared with nothing to fire it"
        )

    def test_a_recipient_without_an_address_is_not_silently_dropped(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session, caplog
    ) -> None:
        """A person who cannot be reached is a person who will not know they
        were not told. It is logged as a warning rather than passing quietly."""
        import logging

        caplog.set_level(logging.WARNING, logger="linx.notifications")

        job = make_open_turnover()
        turnover = db.get(Turnover, uuid.UUID(job["turnover"]["id"]))
        owner = notifications.owner_of(db, turnover)
        owner.email = ""
        db.commit()

        cleaner = make_cleaner(cleared=True)
        _bid(client, cleaner, job["turnover"]["id"])

        assert not _rows(db, NotificationEvent.BID_RECEIVED)
        assert any("no address for recipient" in r.getMessage() for r in caplog.records)


class TestQueueingIsPartOfTheTransaction:
    def test_a_rolled_back_change_leaves_no_notification(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        """The reason queue() does not commit: a message must not describe
        something that then did not happen."""
        job = make_open_turnover()
        turnover = db.get(Turnover, uuid.UUID(job["turnover"]["id"]))
        prop_id = turnover.property_id
        from app.models import Property

        prop = db.get(Property, prop_id)

        turnover.status = TurnoverStatus.CANCELLED
        notifications.turnover_posted(db, turnover, prop)
        db.rollback()

        db.expire_all()
        assert not _rows(db, NotificationEvent.TURNOVER_POSTED)
        assert db.get(Turnover, uuid.UUID(job["turnover"]["id"])).status is TurnoverStatus.OPEN
