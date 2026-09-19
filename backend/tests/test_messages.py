"""Owner and booked cleaner, talking about one job.

**This feature reopened a v1 scope decision**, so what it is allowed to do is
worth testing rather than assuming. "In-app messaging is out of scope; email
and SMS are enough" was right about notifications and wrong about the question
a cleaner standing at a locked gate has — which previously went to a phone
number this product deliberately does not hand out, or went unasked.

Two things it must not do, and most of these tests are about those:

1. **Open a channel that is not a booking.** Bidding is not a relationship. A
   thread exists for a live award and ends when the award does, exactly as the
   street address and the access notes do.
2. **Widen what either side knows about the other.** The owner already sees the
   cleaner's name; the cleaner is never told whose house it is. A thread is the
   easiest place in this product to leak that, and the leak would render
   perfectly.
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Notification
from app.models.enums import NotificationEvent


#: Distinct on purpose: half these tests are about this name NOT appearing
#: where the cleaner can read it, and the fixtures default everybody to the
#: same "Test Person".
OWNER_NAME = "Olivia Ownerson"


def _award_a_job(
    client: TestClient, make_cleaner, make_open_turnover, make_user, **kwargs
) -> dict:
    owner = make_user(role="owner", full_name=OWNER_NAME)
    job = make_open_turnover(owner=owner, **kwargs)
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


def _send(client: TestClient, job: dict, who: str, body: str):
    return client.post(
        f"/api/turnovers/{job['turnover']['id']}/messages",
        json={"body": body},
        headers=job[who]["auth"],
    )


def _read(client: TestClient, job: dict, who: str):
    return client.get(
        f"/api/turnovers/{job['turnover']['id']}/messages", headers=job[who]["auth"]
    )


def _rows(db: Session) -> list[Notification]:
    db.expire_all()
    return list(
        db.execute(
            select(Notification).where(
                Notification.event == NotificationEvent.MESSAGE_RECEIVED
            )
        )
        .scalars()
        .all()
    )


class TestTheConversation:
    def test_both_sides_can_send_and_both_see_it(
        self, client: TestClient, make_cleaner, make_open_turnover, make_user
    ) -> None:
        job = _award_a_job(client, make_cleaner, make_open_turnover, make_user)

        assert _send(client, job, "cleaner", "Which gate do I use?").status_code == 201
        assert _send(client, job, "owner", "The side one, code 4821.").status_code == 201

        for side in ("owner", "cleaner"):
            thread = _read(client, job, side).json()
            assert [m["body"] for m in thread["messages"]] == [
                "Which gate do I use?",
                "The side one, code 4821.",
            ]

    def test_an_action_answers_with_the_whole_thread(
        self, client: TestClient, make_cleaner, make_open_turnover, make_user
    ) -> None:
        """One shape per resource. A POST that answered with just the new
        message would blank a screen that replaced its state with the reply."""
        job = _award_a_job(client, make_cleaner, make_open_turnover, make_user)
        _send(client, job, "cleaner", "first")

        answered = _send(client, job, "owner", "second").json()

        assert [m["body"] for m in answered["messages"]] == ["first", "second"]
        assert answered["can_send"] is True

    def test_an_empty_message_is_refused(
        self, client: TestClient, make_cleaner, make_open_turnover, make_user
    ) -> None:
        job = _award_a_job(client, make_cleaner, make_open_turnover, make_user)

        assert _send(client, job, "owner", "   ").status_code == 422


class TestItDoesNotWidenWhatEitherSideKnows:
    def test_the_cleaner_is_never_shown_the_owners_name(
        self, client: TestClient, make_cleaner, make_open_turnover, make_user, db: Session
    ) -> None:
        """**The failure this feature is most likely to cause, and it would
        render perfectly.** The owner's identity is withheld everywhere else in
        the product; a thread that signed their messages would undo that with
        nothing failing."""
        job = _award_a_job(client, make_cleaner, make_open_turnover, make_user)
        _send(client, job, "owner", "The side gate.")

        thread = _read(client, job, "cleaner")

        assert thread.status_code == 200, thread.text
        labels = [m["sender_label"] for m in thread.json()["messages"]]
        assert labels == ["The owner"]
        owner_name = OWNER_NAME
        assert owner_name not in thread.text
        assert job["owner"]["user"]["email"] not in thread.text

    def test_the_owner_sees_the_cleaner_they_booked_by_name(
        self, client: TestClient, make_cleaner, make_open_turnover, make_user
    ) -> None:
        """The asymmetry is the existing boundary, not a new one — an owner
        already knows which cleaner they booked."""
        job = _award_a_job(client, make_cleaner, make_open_turnover, make_user)
        _send(client, job, "cleaner", "On my way.")

        thread = _read(client, job, "owner").json()

        assert thread["messages"][0]["sender_label"] == job["cleaner"]["user"]["full_name"]

    def test_your_own_messages_read_as_yours(
        self, client: TestClient, make_cleaner, make_open_turnover, make_user
    ) -> None:
        job = _award_a_job(client, make_cleaner, make_open_turnover, make_user)
        _send(client, job, "owner", "hello")

        mine = _read(client, job, "owner").json()["messages"][0]

        assert mine["mine"] is True
        assert mine["sender_label"] == "You"

    def test_no_user_ids_or_addresses_leave_the_endpoint(
        self, client: TestClient, make_cleaner, make_open_turnover, make_user
    ) -> None:
        """An id the frontend could look up is the same leak with a step in
        front of it."""
        job = _award_a_job(client, make_cleaner, make_open_turnover, make_user)
        _send(client, job, "owner", "hello")

        body = _read(client, job, "cleaner").text

        assert "sender_id" not in body
        assert "@" not in body


class TestThereIsNoChannelWithoutABooking:
    def test_a_cleaner_who_has_only_bid_has_no_thread(
        self, client: TestClient, make_cleaner, make_open_turnover, make_user
    ) -> None:
        """**Bidding is not a relationship.** A message box on the board would
        be a channel into somebody's house before anybody agreed to it."""
        job = make_open_turnover()
        bidder = make_cleaner(cleared=True)
        client.put(
            f"/api/board/{job['turnover']['id']}/bid",
            json={"price_cents": 9_000},
            headers=bidder["auth"],
        )

        resp = client.get(
            f"/api/turnovers/{job['turnover']['id']}/messages", headers=bidder["auth"]
        )

        assert resp.status_code == 404

    def test_an_unrelated_cleaner_gets_404_not_403(
        self, client: TestClient, make_cleaner, make_open_turnover, make_user
    ) -> None:
        """A refusal that tells "not yours" from "not there" confirms the id."""
        job = _award_a_job(client, make_cleaner, make_open_turnover, make_user)
        stranger = make_cleaner(cleared=True)

        resp = client.get(
            f"/api/turnovers/{job['turnover']['id']}/messages", headers=stranger["auth"]
        )

        assert resp.status_code == 404

    def test_an_unrelated_owner_gets_404(
        self, client: TestClient, make_cleaner, make_open_turnover, make_user
    ) -> None:
        job = _award_a_job(client, make_cleaner, make_open_turnover, make_user)
        other = make_user(role="owner")

        resp = client.get(
            f"/api/turnovers/{job['turnover']['id']}/messages", headers=other["auth"]
        )

        assert resp.status_code == 404

    def test_the_thread_closes_when_the_booking_is_cancelled(
        self, client: TestClient, make_cleaner, make_open_turnover, make_user, db: Session
    ) -> None:
        """Access follows the live award, the same line the street address and
        the access notes follow. A thread left open would be a channel to a
        stranger's house outliving the reason it was opened."""
        job = _award_a_job(client, make_cleaner, make_open_turnover, make_user)
        _send(client, job, "owner", "See you at 11.")

        client.post(
            f"/api/board/jobs/{job['turnover']['id']}/cancel",
            json={"reason": "van broke down"},
            headers=job["cleaner"]["auth"],
        )

        assert _read(client, job, "cleaner").status_code == 404
        assert _read(client, job, "owner").status_code == 404
        assert _send(client, job, "cleaner", "actually...").status_code == 404

    def test_an_admin_is_not_a_party_to_it(
        self, client: TestClient, make_cleaner, make_open_turnover, make_user, admin_user
    ) -> None:
        """The console reads what it needs from its own endpoints. A private
        conversation is not an admin screen by default — if a dispute needs it,
        that is a decision to make out loud."""
        job = _award_a_job(client, make_cleaner, make_open_turnover, make_user)
        _send(client, job, "owner", "hello")

        resp = client.get(
            f"/api/turnovers/{job['turnover']['id']}/messages",
            headers=admin_user["auth"],
        )

        assert resp.status_code == 404


class TestTheOtherSideIsTold:
    def test_sending_notifies_the_other_side_only(
        self, client: TestClient, make_cleaner, make_open_turnover, make_user, db: Session
    ) -> None:
        """**The test the closed list requires.** There is no push channel
        here, so the email is the whole delivery mechanism — delete the queue
        call and the thread still works perfectly while nobody ever answers."""
        job = _award_a_job(client, make_cleaner, make_open_turnover, make_user)

        _send(client, job, "cleaner", "Which gate?")

        rows = _rows(db)
        assert len(rows) == 1
        assert rows[0].recipient_id == uuid.UUID(job["owner"]["user"]["id"])

    def test_each_message_is_its_own_notification(
        self, client: TestClient, make_cleaner, make_open_turnover, make_user, db: Session
    ) -> None:
        """Keyed on the message rather than the transition, unlike every other
        event here: each message genuinely is a separate thing to be told."""
        job = _award_a_job(client, make_cleaner, make_open_turnover, make_user)

        _send(client, job, "cleaner", "one")
        _send(client, job, "cleaner", "two")

        assert len(_rows(db)) == 2

    def test_the_notification_does_not_name_the_owner_either(
        self, client: TestClient, make_cleaner, make_open_turnover, make_user, db: Session
    ) -> None:
        """The boundary does not stop at the API. An email signed with the
        owner's name is the same leak by a different route."""
        job = _award_a_job(client, make_cleaner, make_open_turnover, make_user)

        _send(client, job, "owner", "The side gate.")

        row = _rows(db)[0]
        assert OWNER_NAME not in row.body
        assert "The owner" in row.body
