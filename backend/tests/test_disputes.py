"""Phase 8 — disputes, and the console a person works them in.

**The one thing in this product deliberately not resolved by code.** A dispute
is a disagreement between two people that a human decides; the table exists so
the decision has somewhere to live and something to be argued from later
(CLAUDE.md: "Disputes go to a human inbox, not a bot, at v1").

Three groups of assertion here, and the middle one is the load-bearing group:

1. Who may raise one, and what a malformed or duplicate attempt does.
2. **Who hears about it, and — more importantly — who does not.** Raising a
   dispute tells an admin and the person who raised it, and deliberately *not*
   the other side: at this size a human decides when to involve somebody in a
   complaint about them, and a system that forwards it automatically has
   replaced the judgement the inbox exists for.
3. The console: admin-only, ordered the way a person works a queue, and
   reading its numbers from the same definitions the rest of the product uses
   rather than carrying its own copies.
"""

from __future__ import annotations

import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.main import app
from app.models import NotificationEvent
from app.models.notification import Notification


def _bid(client: TestClient, cleaner: dict, turnover_id: str, cents: int = 15_000) -> dict:
    resp = client.put(
        f"/api/board/{turnover_id}/bid",
        json={"price_cents": cents, "message": "On it."},
        headers=cleaner["auth"],
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _awarded_job(client: TestClient, make_cleaner, make_open_turnover) -> dict:
    """Bid → award. The state a dispute hangs off: two people, one job."""
    job = make_open_turnover()
    cleaner = make_cleaner(cleared=True)
    bid = _bid(client, cleaner, job["turnover"]["id"])
    accepted = client.post(
        f"/api/turnovers/{job['turnover']['id']}/bids/{bid['id']}/accept",
        headers=job["owner"]["auth"],
    )
    assert accepted.status_code == 200, accepted.text
    job["cleaner"] = cleaner
    return job


def _raise(client: TestClient, who: dict, turnover_id: str, **overrides):
    payload = {"reason": "quality", "description": "The kitchen was not touched."}
    payload.update(overrides)
    return client.post(
        f"/api/turnovers/{turnover_id}/disputes", json=payload, headers=who["auth"]
    )


def _raised(client: TestClient, who: dict, turnover_id: str, **overrides) -> dict:
    """Raise one and hand back the row. The endpoint answers with the whole
    shape — one shape per resource — so the new row is dug out of `mine`."""
    resp = _raise(client, who, turnover_id, **overrides)
    assert resp.status_code == 201, resp.text
    return resp.json()["mine"][-1]


def _events(db: Session, event: NotificationEvent) -> list[Notification]:
    return list(
        db.execute(
            select(Notification).where(Notification.event == event)
        ).scalars().all()
    )


# --------------------------------------------------------------------------
# Who may raise one
# --------------------------------------------------------------------------


class TestRaisingADispute:
    def test_either_side_of_a_booked_job_can_raise_one(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        job = _awarded_job(client, make_cleaner, make_open_turnover)

        by_owner = _raise(client, job["owner"], job["turnover"]["id"])
        assert by_owner.status_code == 201, by_owner.text
        # Answers with the same shape the GET does — "one shape per resource",
        # so the screen keeps what it was showing and picks the new row up in
        # one round trip.
        assert by_owner.json()["mine"][0]["raised_by_role"] == "owner"

        by_cleaner = _raise(
            client, job["cleaner"], job["turnover"]["id"], reason="access"
        )
        assert by_cleaner.status_code == 201, by_cleaner.text
        assert by_cleaner.json()["mine"][0]["raised_by_role"] == "cleaner"

    def test_a_stranger_gets_404_rather_than_403(
        self, client: TestClient, make_cleaner, make_open_turnover, make_user
    ) -> None:
        """**The repo's rule everywhere: 403 confirms the id exists.**"""
        job = _awarded_job(client, make_cleaner, make_open_turnover)
        nosy = make_user(role="owner")

        resp = _raise(client, nosy, job["turnover"]["id"])
        assert resp.status_code == 404, resp.text

        reading = client.get(
            f"/api/turnovers/{job['turnover']['id']}/disputes", headers=nosy["auth"]
        )
        assert reading.status_code == 404

    def test_a_job_nobody_was_booked_for_is_refused_with_a_reason(
        self, client: TestClient, make_open_turnover
    ) -> None:
        """There is no one to raise it *with*. The message says what to do
        instead rather than just refusing."""
        job = make_open_turnover()
        resp = _raise(client, job["owner"], job["turnover"]["id"])
        assert resp.status_code == 409, resp.text
        assert "cancel it instead" in resp.json()["detail"]

    def test_an_empty_description_is_refused(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        job = _awarded_job(client, make_cleaner, make_open_turnover)
        resp = _raise(client, job["owner"], job["turnover"]["id"], description="   ")
        assert resp.status_code in (409, 422), resp.text

    def test_a_second_open_dispute_from_the_same_person_is_refused(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        """A courtesy rather than an invariant — the service says so, and says
        why it takes no lock. What it prevents is one person filling the queue
        with the same complaint."""
        job = _awarded_job(client, make_cleaner, make_open_turnover)
        assert _raise(client, job["owner"], job["turnover"]["id"]).status_code == 201

        again = _raise(client, job["owner"], job["turnover"]["id"])
        assert again.status_code == 409
        assert "already have an open dispute" in again.json()["detail"]

    def test_the_other_side_can_still_raise_their_own(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        """The guard is per person, not per job. Both sides being unhappy is
        two complaints, not one."""
        job = _awarded_job(client, make_cleaner, make_open_turnover)
        assert _raise(client, job["owner"], job["turnover"]["id"]).status_code == 201
        assert _raise(client, job["cleaner"], job["turnover"]["id"]).status_code == 201


# --------------------------------------------------------------------------
# Who hears — and who does not
# --------------------------------------------------------------------------


class TestWhoIsTold:
    def test_raising_tells_an_admin_and_the_raiser_but_not_the_other_side(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session,
        admin_user,
    ) -> None:
        """**The assertion this whole feature turns on.**

        A complaint nobody is told about is the same as no complaint. But the
        person being complained about is deliberately *not* told yet: a human
        decides when to involve them, which is the entire reason the inbox is a
        person rather than a workflow. A system that forwards it automatically
        has already made that decision, badly, for every case.
        """
        job = _awarded_job(client, make_cleaner, make_open_turnover)
        assert _raise(client, job["owner"], job["turnover"]["id"]).status_code == 201

        told = {n.recipient_id for n in _events(db, NotificationEvent.DISPUTE_RAISED)}
        owner_id = uuid.UUID(job["owner"]["user"]["id"])
        cleaner_id = uuid.UUID(job["cleaner"]["user"]["id"])

        assert owner_id in told, "the person who raised it needs a receipt"
        assert admin_user["user"].id in told, "somebody has to work the queue"
        assert cleaner_id not in told, (
            "the cleaner must not learn they are being complained about until "
            "a person decides to involve them"
        )

    def test_acknowledging_tells_nobody(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session,
        admin_user,
    ) -> None:
        """"A person has picked this up" and "this is settled" are different
        facts. The first shows on the raiser's own view of the dispute; it is
        not worth an email, and sending one would train people to ignore the
        one that matters."""
        job = _awarded_job(client, make_cleaner, make_open_turnover)
        raised = _raised(client, job["owner"], job["turnover"]["id"])
        before = len(_events(db, NotificationEvent.DISPUTE_RAISED))

        ack = client.post(
            f"/api/admin/disputes/{raised['id']}/acknowledge",
            headers=admin_user["auth"],
        )
        assert ack.status_code == 200, ack.text
        assert ack.json()["status"] == "acknowledged"

        db.expire_all()
        assert _events(db, NotificationEvent.DISPUTE_RESOLVED) == []
        assert len(_events(db, NotificationEvent.DISPUTE_RAISED)) == before

    def test_resolving_tells_both_sides_and_carries_the_note(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session,
        admin_user,
    ) -> None:
        """Both parties, because the other side may be learning a dispute
        existed at all — and a bare "resolved" to somebody who did not know
        they were being complained about is worse than silence. The note is
        quoted as written."""
        job = _awarded_job(client, make_cleaner, make_open_turnover)
        raised = _raised(client, job["owner"], job["turnover"]["id"])

        note = "Spoke to both. Cleaner returning Thursday at no charge."
        resolved = client.post(
            f"/api/admin/disputes/{raised['id']}/resolve",
            json={"notes": note},
            headers=admin_user["auth"],
        )
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["status"] == "resolved"

        db.expire_all()
        rows = _events(db, NotificationEvent.DISPUTE_RESOLVED)
        told = {n.recipient_id for n in rows}
        assert uuid.UUID(job["owner"]["user"]["id"]) in told
        assert uuid.UUID(job["cleaner"]["user"]["id"]) in told
        assert any(note in n.body for n in rows), "the decision reaches them verbatim"

    def test_resolving_without_a_note_is_refused(
        self, client: TestClient, make_cleaner, make_open_turnover, admin_user
    ) -> None:
        """The note *is* the outcome — there is no `rejected` status, because
        the outcomes at this size are not an enumerable set. Resolving without
        one would record that something was decided and not what."""
        job = _awarded_job(client, make_cleaner, make_open_turnover)
        raised = _raised(client, job["owner"], job["turnover"]["id"])

        resp = client.post(
            f"/api/admin/disputes/{raised['id']}/resolve",
            json={"notes": "   "},
            headers=admin_user["auth"],
        )
        assert resp.status_code in (409, 422), resp.text


# --------------------------------------------------------------------------
# What each side can see
# --------------------------------------------------------------------------


class TestWhatAPartyCanSee:
    def test_a_party_sees_their_own_disputes_and_not_the_other_side_s(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        """Not the content and **not the existence**. A count on this screen
        would decide, for the admin, whether somebody gets told they are being
        complained about."""
        job = _awarded_job(client, make_cleaner, make_open_turnover)
        assert _raise(
            client, job["cleaner"], job["turnover"]["id"],
            reason="access", description="Lockbox code was wrong.",
        ).status_code == 201

        seen = client.get(
            f"/api/turnovers/{job['turnover']['id']}/disputes",
            headers=job["owner"]["auth"],
        )
        assert seen.status_code == 200, seen.text
        body = seen.json()
        assert body["mine"] == []
        assert "Lockbox" not in seen.text
        assert body["can_raise"] is True

    def test_the_raiser_sees_the_outcome_on_their_own_view(
        self, client: TestClient, make_cleaner, make_open_turnover, admin_user
    ) -> None:
        job = _awarded_job(client, make_cleaner, make_open_turnover)
        raised = _raised(client, job["owner"], job["turnover"]["id"])
        client.post(
            f"/api/admin/disputes/{raised['id']}/resolve",
            json={"notes": "Refunded in full."},
            headers=admin_user["auth"],
        )

        mine = client.get(
            f"/api/turnovers/{job['turnover']['id']}/disputes",
            headers=job["owner"]["auth"],
        ).json()["mine"]
        assert len(mine) == 1
        assert mine[0]["status"] == "resolved"
        assert mine[0]["resolution_notes"] == "Refunded in full."


# --------------------------------------------------------------------------
# The console
# --------------------------------------------------------------------------


class TestTheConsoleIsAdminOnly:
    @pytest.mark.parametrize(
        "path",
        ["/api/admin/summary", "/api/admin/disputes", "/api/admin/unclaimed",
         "/api/admin/ledger"],
    )
    def test_an_owner_cannot_reach_it(
        self, client: TestClient, make_user, path: str
    ) -> None:
        owner = make_user(role="owner")
        assert client.get(path, headers=owner["auth"]).status_code == 403

    @pytest.mark.parametrize(
        "path",
        ["/api/admin/summary", "/api/admin/disputes", "/api/admin/unclaimed",
         "/api/admin/ledger"],
    )
    def test_a_cleaner_cannot_reach_it(
        self, client: TestClient, make_cleaner, path: str
    ) -> None:
        cleaner = make_cleaner(cleared=True)
        assert client.get(path, headers=cleaner["auth"]).status_code == 403

    def test_signed_out_is_401_not_403(self, client: TestClient) -> None:
        assert client.get("/api/admin/summary").status_code == 401


class TestTheInbox:
    def test_a_settled_dispute_leaves_the_queue(
        self, client: TestClient, make_cleaner, make_open_turnover, admin_user
    ) -> None:
        """A queue shows work, not history. Resolved ones are still reachable
        by id and by asking for them — they are the record a later argument is
        had from — but they are not what somebody opening the inbox is for."""
        job = _awarded_job(client, make_cleaner, make_open_turnover)
        raised = _raised(client, job["owner"], job["turnover"]["id"])

        client.post(
            f"/api/admin/disputes/{raised['id']}/resolve",
            json={"notes": "Sorted."},
            headers=admin_user["auth"],
        )

        queue = client.get("/api/admin/disputes", headers=admin_user["auth"]).json()
        assert raised["id"] not in [row["id"] for row in queue]

        with_history = client.get(
            "/api/admin/disputes?include_resolved=true", headers=admin_user["auth"]
        ).json()
        assert raised["id"] in [row["id"] for row in with_history]

    def test_open_ones_come_before_settled_ones_and_oldest_first(
        self, client: TestClient, make_cleaner, make_open_turnover, admin_user
    ) -> None:
        """**The order a person works a queue in**, and the order the
        `(status, created_at)` index is built for. Newest-first would bury the
        complaint that has waited longest under the one that arrived this
        morning — and the one waiting longest is the one about to become a
        phone call."""
        first = _awarded_job(client, make_cleaner, make_open_turnover)
        second = _awarded_job(client, make_cleaner, make_open_turnover)
        third = _awarded_job(client, make_cleaner, make_open_turnover)

        settled = _raised(client, first["owner"], first["turnover"]["id"])
        older = _raised(client, second["owner"], second["turnover"]["id"])
        newer = _raised(client, third["owner"], third["turnover"]["id"])

        client.post(
            f"/api/admin/disputes/{settled['id']}/resolve",
            json={"notes": "Sorted."},
            headers=admin_user["auth"],
        )

        ids = [
            row["id"]
            for row in client.get(
                "/api/admin/disputes?include_resolved=true", headers=admin_user["auth"]
            ).json()
        ]
        assert ids.index(older["id"]) < ids.index(newer["id"]), "oldest first"
        assert ids.index(newer["id"]) < ids.index(settled["id"]), (
            "anything still open outranks anything already decided"
        )

    def test_it_names_both_parties_because_somebody_has_to_contact_them(
        self, client: TestClient, make_cleaner, make_open_turnover, admin_user
    ) -> None:
        """The privacy boundary that withholds identity from *cleaners* does
        not apply to the person deciding the complaint — they cannot resolve it
        without being able to reach both sides."""
        job = _awarded_job(client, make_cleaner, make_open_turnover)
        raised = _raised(client, job["owner"], job["turnover"]["id"])

        row = client.get(
            f"/api/admin/disputes/{raised['id']}", headers=admin_user["auth"]
        ).json()
        assert row["owner"]["email"] == job["owner"]["user"]["email"]
        assert row["cleaner"]["email"] == job["cleaner"]["user"]["email"]


class TestTheConsoleReadsSharedDefinitions:
    def test_unclaimed_matches_the_alarm_that_sends_the_email(
        self, client: TestClient, make_open_turnover, admin_user, db: Session
    ) -> None:
        """**One definition, two readers.** An admin screen that disagrees with
        the alert an admin was sent is worse than either alone, because it makes
        both untrustworthy and there is no way to tell which is lying."""
        from app.services import turnovers as turnover_rules

        make_open_turnover(days_out=1)
        make_open_turnover(days_out=30)

        screen = client.get(
            "/api/admin/unclaimed", headers=admin_user["auth"]
        ).json()
        from_the_definition = turnover_rules.unclaimed_alarming(db)

        assert {row["turnover_id"] for row in screen} == {
            str(turnover.id) for turnover, _ in from_the_definition
        }

    # The ledger's own assertion lives in `test_payments.py`, with the Stripe
    # fixtures that can actually settle a payment — an empty ledger satisfies
    # the arithmetic without testing it, which is the failure this suite has
    # already walked into more than once.

    def test_the_summary_counts_what_the_queues_hold(
        self, client: TestClient, make_cleaner, make_open_turnover, admin_user
    ) -> None:
        job = _awarded_job(client, make_cleaner, make_open_turnover)
        _raise(client, job["owner"], job["turnover"]["id"])

        summary = client.get(
            "/api/admin/summary", headers=admin_user["auth"]
        ).json()
        assert summary["open_disputes"] == 1


# --------------------------------------------------------------------------
# A dispute is about one booking, and a turnover can have several
# --------------------------------------------------------------------------


def _re_award(client: TestClient, job: dict, make_cleaner) -> dict:
    """Cancel the live booking and award the re-posted job to somebody else.

    An ordinary supported sequence: `awards.py` puts a cancelled turnover back
    to `open`, the bench picks it up, and the next accept writes a *second*
    `Award` row alongside the cancelled first one.
    """
    turnover_id = job["turnover"]["id"]
    cancelled = client.post(
        f"/api/board/jobs/{turnover_id}/cancel",
        json={"reason": "Van is off the road."},
        headers=job["cleaner"]["auth"],
    )
    assert cancelled.status_code == 200, cancelled.text

    replacement = make_cleaner(cleared=True)
    bid = _bid(client, replacement, turnover_id, cents=16_000)
    accepted = client.post(
        f"/api/turnovers/{turnover_id}/bids/{bid['id']}/accept",
        headers=job["owner"]["auth"],
    )
    assert accepted.status_code == 200, accepted.text
    return replacement


class TestADisputeStaysWithItsOwnAward:
    """**The bug this class exists for was silent and pointed at a stranger.**

    `parties` used to answer "the most recent award on this turnover", read
    fresh every time anything asked. A turnover that had been cancelled and
    re-awarded therefore handed every existing dispute to the replacement
    cleaner: the console showed their name and phone number on somebody else's
    complaint, resolving emailed them about it, and the cleaner who actually
    raised it got a 404 on their own dispute and no resolution.

    Nothing failed. The rows were all valid; they just described the wrong
    person.
    """

    def test_a_superseded_cleaner_can_still_raise_one(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        job = _awarded_job(client, make_cleaner, make_open_turnover)
        first = job["cleaner"]
        _re_award(client, job, make_cleaner)

        # They were booked for this job and lost it. That is the complaint.
        resp = _raise(client, first, job["turnover"]["id"])
        assert resp.status_code == 201, resp.text

    def test_it_keeps_naming_the_cleaner_it_was_raised_against(
        self, client: TestClient, make_cleaner, make_open_turnover, admin_user
    ) -> None:
        job = _awarded_job(client, make_cleaner, make_open_turnover)
        first = job["cleaner"]
        raised = _raised(client, job["owner"], job["turnover"]["id"])

        replacement = _re_award(client, job, make_cleaner)

        admin = admin_user
        inbox = client.get("/api/admin/disputes", headers=admin["auth"])
        assert inbox.status_code == 200, inbox.text
        row = next(d for d in inbox.json() if d["id"] == raised["id"])

        assert row["cleaner"]["email"] == first["user"]["email"], (
            "the console showed the replacement cleaner's identity and contact "
            "details on a complaint that was not about them"
        )
        assert row["cleaner"]["email"] != replacement["user"]["email"]

    def test_resolving_tells_the_cleaner_it_was_about(
        self, client: TestClient, make_cleaner, make_open_turnover, admin_user,
        db: Session,
    ) -> None:
        job = _awarded_job(client, make_cleaner, make_open_turnover)
        first = job["cleaner"]
        raised = _raised(client, job["owner"], job["turnover"]["id"])

        replacement = _re_award(client, job, make_cleaner)

        admin = admin_user
        resolved = client.post(
            f"/api/admin/disputes/{raised['id']}/resolve",
            json={"notes": "Spoke to both."},
            headers=admin["auth"],
        )
        assert resolved.status_code == 200, resolved.text

        told = {
            n.destination
            for n in _events(db, NotificationEvent.DISPUTE_RESOLVED)
        }
        assert first["user"]["email"] in told
        assert replacement["user"]["email"] not in told, (
            "an uninvolved cleaner was emailed about somebody else's dispute"
        )

    def test_the_raiser_keeps_reading_their_own_dispute(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        job = _awarded_job(client, make_cleaner, make_open_turnover)
        first = job["cleaner"]
        raised = _raised(client, first, job["turnover"]["id"])

        _re_award(client, job, make_cleaner)

        mine = client.get(
            f"/api/turnovers/{job['turnover']['id']}/disputes",
            headers=first["auth"],
        )
        assert mine.status_code == 200, mine.text
        assert [d["id"] for d in mine.json()["mine"]] == [raised["id"]], (
            "the cleaner who raised it was locked out of their own complaint"
        )


class TestTheFilerSaysWhichBooking:
    """**Freezing the award stopped a dispute changing who it was about; it
    did not make an inferred choice right in the first place.**

    A turnover can carry several bookings, and only the person filing knows
    which one went wrong. Picking the newest files an owner's complaint about
    last month's no-show against the cleaner who took the re-posted job — who
    has done nothing — and the console and the resolution email both name them.
    """

    def test_it_refuses_to_guess_between_two_bookings(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        job = _awarded_job(client, make_cleaner, make_open_turnover)
        _re_award(client, job, make_cleaner)

        resp = _raise(client, job["owner"], job["turnover"]["id"])
        assert resp.status_code == 409, resp.text
        assert "which booking" in resp.json()["detail"].lower()

    def test_the_owner_can_name_the_booking_that_went_wrong(
        self, client: TestClient, make_cleaner, make_open_turnover, admin_user,
        db: Session,
    ) -> None:
        job = _awarded_job(client, make_cleaner, make_open_turnover)
        first = job["cleaner"]
        replacement = _re_award(client, job, make_cleaner)

        # The screen offers both, newest first, and says what became of each.
        state = client.get(
            f"/api/turnovers/{job['turnover']['id']}/disputes",
            headers=job["owner"]["auth"],
        ).json()
        assert len(state["bookings"]) == 2
        older = state["bookings"][-1]
        assert older["cleaner_name"] == first["user"]["full_name"]
        assert older["cancelled_at"] is not None

        raised = _raised(
            client, job["owner"], job["turnover"]["id"], award_id=older["award_id"]
        )

        row = next(
            d
            for d in client.get("/api/admin/disputes", headers=admin_user["auth"]).json()
            if d["id"] == raised["id"]
        )
        assert row["cleaner"]["email"] == first["user"]["email"]

        client.post(
            f"/api/admin/disputes/{raised['id']}/resolve",
            json={"notes": "Spoke to both."},
            headers=admin_user["auth"],
        )
        told = {n.destination for n in _events(db, NotificationEvent.DISPUTE_RESOLVED)}
        assert first["user"]["email"] in told
        assert replacement["user"]["email"] not in told

    def test_a_booking_that_is_not_yours_is_refused(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        job = _awarded_job(client, make_cleaner, make_open_turnover)
        first = job["cleaner"]
        _re_award(client, job, make_cleaner)

        # The owner can see both; the first cleaner may only name their own.
        bookings = client.get(
            f"/api/turnovers/{job['turnover']['id']}/disputes",
            headers=job["owner"]["auth"],
        ).json()["bookings"]
        newest = bookings[0]

        resp = _raise(
            client, first, job["turnover"]["id"], award_id=newest["award_id"]
        )
        assert resp.status_code == 409, resp.text
        assert "not one of yours" in resp.json()["detail"]

    def test_a_cleaner_booked_twice_is_asked_which_time(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        """Backing out and later winning the re-posted job is two awards, both
        theirs. Neither the server nor the screen can tell which one the
        complaint is about."""
        job = _awarded_job(client, make_cleaner, make_open_turnover)
        cleaner = job["cleaner"]
        turnover_id = job["turnover"]["id"]

        cancelled = client.post(
            f"/api/board/jobs/{turnover_id}/cancel",
            json={"reason": "Van is off the road."},
            headers=cleaner["auth"],
        )
        assert cancelled.status_code == 200, cancelled.text

        bid = _bid(client, cleaner, turnover_id, cents=17_000)
        accepted = client.post(
            f"/api/turnovers/{turnover_id}/bids/{bid['id']}/accept",
            headers=job["owner"]["auth"],
        )
        assert accepted.status_code == 200, accepted.text

        state = client.get(
            f"/api/turnovers/{turnover_id}/disputes", headers=cleaner["auth"]
        ).json()
        assert len(state["bookings"]) == 2, (
            "both awards are this cleaner's own, and only they know which one "
            "the complaint is about"
        )

        assert _raise(client, cleaner, turnover_id).status_code == 409
        assert (
            _raise(
                client,
                cleaner,
                turnover_id,
                award_id=state["bookings"][-1]["award_id"],
            ).status_code
            == 201
        )


class TestTwoAdminsAtOnce:
    """Working a dispute is serialised on the row; raising one is not.

    The asymmetry is deliberate and is the difference between a duplicate
    somebody closes and a record that disagrees with what the parties were
    told.
    """

    def test_acknowledging_cannot_reopen_a_settled_dispute(
        self, client: TestClient, make_cleaner, make_open_turnover, admin_user,
        db: Session,
    ) -> None:
        job = _awarded_job(client, make_cleaner, make_open_turnover)
        raised = _raised(client, job["owner"], job["turnover"]["id"])

        resolved = client.post(
            f"/api/admin/disputes/{raised['id']}/resolve",
            json={"notes": "Settled."},
            headers=admin_user["auth"],
        )
        assert resolved.status_code == 200, resolved.text

        # The second admin's click lands after the first one's write.
        late = client.post(
            f"/api/admin/disputes/{raised['id']}/acknowledge",
            headers=admin_user["auth"],
        )
        assert late.status_code == 409, (
            "acknowledging wrote `acknowledged` back over a resolved dispute, "
            "leaving the resolution note on a row that said nobody had settled "
            "it — after both parties had been told it was settled"
        )

        from app.models.dispute import Dispute

        row = db.get(Dispute, uuid.UUID(raised["id"]))
        db.refresh(row)
        assert row.status.value == "resolved"
        assert row.resolution_notes == "Settled."

    def test_two_simultaneous_resolves_agree_with_what_was_sent(
        self,
        client: TestClient,
        make_cleaner,
        make_open_turnover,
        admin_user,
        db: Session,
        own_session_per_request,
    ) -> None:
        """**The real race, not a sequential stand-in.**

        The test below asserts the refusal exists; this one asserts the lock
        makes it trustworthy. Two admins press Resolve at the same instant with
        different notes. Unlocked, both read `open`, both write, the row keeps
        the last note and the dedupe key had already queued the first — so the
        record disagrees with the message. Locked, the second sees a resolved
        dispute and is refused.
        """
        job = _awarded_job(client, make_cleaner, make_open_turnover)
        raised = _raised(client, job["owner"], job["turnover"]["id"])
        auth = admin_user["auth"]
        dispute_id = raised["id"]

        # Release the setup session's snapshot and any locks it holds.
        db.commit()

        start = threading.Barrier(2)
        results: dict[str, int] = {}

        def settle(name: str, note: str) -> None:
            with TestClient(app) as racer:
                start.wait(timeout=10)
                resp = racer.post(
                    f"/api/admin/disputes/{dispute_id}/resolve",
                    json={"notes": note},
                    headers=auth,
                )
                results[name] = resp.status_code

        threads = [
            threading.Thread(target=settle, args=("first", "Cleaner returns Thursday.")),
            threading.Thread(target=settle, args=("second", "Full refund instead.")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            assert not thread.is_alive(), "a resolve never returned — deadlock?"

        assert sorted(results.values()) == [200, 409], results

        from app.models.dispute import Dispute

        db.expire_all()
        row = db.get(Dispute, uuid.UUID(dispute_id))
        sent = [n.body for n in _events(db, NotificationEvent.DISPUTE_RESOLVED)]
        assert len(sent) == 2, "both parties, once each"
        assert row.resolution_notes is not None
        for body in sent:
            assert row.resolution_notes in body, (
                "the note on the row is not the note the parties were sent"
            )

    def test_the_stored_note_is_the_note_that_was_sent(
        self, client: TestClient, make_cleaner, make_open_turnover, admin_user,
        db: Session,
    ) -> None:
        """The record a dispute is argued from later must say what the people
        involved actually received."""
        job = _awarded_job(client, make_cleaner, make_open_turnover)
        raised = _raised(client, job["owner"], job["turnover"]["id"])

        first = client.post(
            f"/api/admin/disputes/{raised['id']}/resolve",
            json={"notes": "Cleaner returning Thursday."},
            headers=admin_user["auth"],
        )
        assert first.status_code == 200, first.text

        second = client.post(
            f"/api/admin/disputes/{raised['id']}/resolve",
            json={"notes": "Full refund instead."},
            headers=admin_user["auth"],
        )
        assert second.status_code == 409, (
            "a second resolve overwrote the note on the row while the dedupe "
            "key had already sent the first one"
        )

        from app.models.dispute import Dispute

        row = db.get(Dispute, uuid.UUID(raised["id"]))
        db.refresh(row)
        sent = [n.body for n in _events(db, NotificationEvent.DISPUTE_RESOLVED)]
        assert row.resolution_notes == "Cleaner returning Thursday."
        assert all("Full refund instead" not in body for body in sent)
        assert any("Cleaner returning Thursday" in body for body in sent)


class TestTheUnclaimedAlarmCountsOffers:
    def test_it_counts_only_bids_the_owner_could_accept(
        self, client: TestClient, make_cleaner, make_open_turnover, admin_user
    ) -> None:
        """**A job re-posted after a cancellation still carries its old bids.**

        Counting them made the console say "3 bids, none accepted" — an owner
        dithering over offers — when there were no live offers at all and the
        real problem was that nobody had bid. An operational alarm that
        misdescribes the problem is worse than one that does not fire.
        """
        from datetime import datetime, timedelta, timezone as tz

        job = _awarded_job(client, make_cleaner, make_open_turnover)
        turnover_id = job["turnover"]["id"]

        cancelled = client.post(
            f"/api/board/jobs/{turnover_id}/cancel",
            json={"reason": "Van is off the road."},
            headers=job["cleaner"]["auth"],
        )
        assert cancelled.status_code == 200, cancelled.text

        # Bring checkout inside the alarm's window so the row shows up.
        from app.models.turnover import Turnover
        from app.db import SessionLocal

        with SessionLocal() as session:
            row = session.get(Turnover, uuid.UUID(turnover_id))
            row.checkout_at = datetime.now(tz.utc) + timedelta(hours=2)
            session.commit()

        rows = client.get("/api/admin/unclaimed", headers=admin_user["auth"]).json()
        mine = next(r for r in rows if r["turnover_id"] == turnover_id)
        assert mine["bid_count"] == 0, (
            "the accepted-then-cancelled bid was counted as an offer the owner "
            "could still choose"
        )
