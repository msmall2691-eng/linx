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

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

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
