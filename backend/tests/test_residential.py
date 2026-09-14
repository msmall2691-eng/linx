"""Homes as well as rentals — a second kind of property, and a scope of work.

**This is the feature that reopened a closed scope.** "Non-STR recurring
residential cleaning" was on CLAUDE.md's v1 out-of-scope list from day one, for
a real reason: a short-term rental's clean is defined by the gap between one
guest leaving and the next arriving, and that window is what the whole urgency
ladder measures. A home has no such window.

What makes it cheap rather than a rewrite is that the model already had the
shape. A turnover with `checkin_at IS NULL` is a standing vacancy, and its
urgency is already measured as *time until the job* rather than the length of a
window. That is exactly what a scheduled house clean is — so residential needs
no second ladder and no second table.

The tests here are mostly about the seams that creates: a home must not acquire
a checkin, a rental must not acquire a deep clean, and neither must quietly
become the other.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import (
    PropertyType,
    ServiceType,
    TurnoverStatus,
    TurnoverUrgency,
)
from app.models.property import Property
from app.models.turnover import Turnover
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


def _post_job(client: TestClient, owner: dict, property_id: str, **overrides):
    payload = {
        "property_id": property_id,
        "checkout_at": (datetime.now(timezone.utc) + timedelta(days=5)).isoformat(),
        **overrides,
    }
    return client.post("/api/turnovers", json=payload, headers=owner["auth"])


class TestPropertyType:
    def test_a_property_is_a_rental_unless_it_says_otherwise(
        self, client: TestClient, make_user
    ) -> None:
        """**Every property that already exists is a rental**, and the default
        has to agree with that or the migration quietly relabels them."""
        owner = make_user(role="owner")
        created = _property(client, owner)
        assert created["property_type"] == "short_term_rental"

    def test_a_home_can_be_added(self, client: TestClient, make_user) -> None:
        owner = make_user(role="owner")
        created = _property(client, owner, property_type="residential")
        assert created["property_type"] == "residential"

    def test_square_footage_is_optional_and_kept(
        self, client: TestClient, make_user
    ) -> None:
        """Plenty of owners do not know it, and a number somebody guessed at is
        worse than no number — but a cleaner prices on it when it is there."""
        owner = make_user(role="owner")
        assert _property(client, owner)["square_feet"] is None
        assert _property(client, owner, square_feet=1800)["square_feet"] == 1800

    def test_nonsense_square_footage_is_refused(
        self, client: TestClient, make_user
    ) -> None:
        owner = make_user(role="owner")
        resp = client.post(
            "/api/properties",
            json={
                "nickname": "Impossible",
                "address_line1": "1 Main St",
                "city": "Portland",
                "state": "ME",
                "postal_code": "04101",
                "square_feet": 0,
            },
            headers=owner["auth"],
        )
        assert resp.status_code == 422


class TestWhatFitsWhat:
    """One place decides, and it refuses rather than correcting."""

    def test_a_home_gets_a_standard_clean_by_default(
        self, client: TestClient, make_user
    ) -> None:
        owner = make_user(role="owner")
        home = _property(client, owner, property_type="residential")
        resp = _post_job(client, owner, home["id"])
        assert resp.status_code == 201, resp.text
        assert resp.json()["service_type"] == "standard"

    def test_a_rental_gets_a_turnover_by_default(
        self, client: TestClient, make_user
    ) -> None:
        owner = make_user(role="owner")
        rental = _property(client, owner)
        resp = _post_job(client, owner, rental["id"])
        assert resp.json()["service_type"] == "turnover"

    @pytest.mark.parametrize("scope", ["standard", "deep", "move_out"])
    def test_a_home_takes_any_of_the_house_scopes(
        self, client: TestClient, make_user, scope: str
    ) -> None:
        owner = make_user(role="owner")
        home = _property(client, owner, property_type="residential")
        resp = _post_job(client, owner, home["id"], service_type=scope)
        assert resp.status_code == 201, resp.text
        assert resp.json()["service_type"] == scope

    def test_a_rental_cannot_be_given_a_deep_clean(
        self, client: TestClient, make_user
    ) -> None:
        """**Refused, not silently corrected.**

        An owner who asked for a move-out clean and got a turnover instead
        finds out from the cleaner who turned up expecting two hours' work.
        """
        owner = make_user(role="owner")
        rental = _property(client, owner)
        resp = _post_job(client, owner, rental["id"], service_type="deep")
        assert resp.status_code == 409
        assert "not deep" in resp.json()["detail"]

    def test_a_home_cannot_be_given_a_turnover(
        self, client: TestClient, make_user
    ) -> None:
        owner = make_user(role="owner")
        home = _property(client, owner, property_type="residential")
        resp = _post_job(client, owner, home["id"], service_type="turnover")
        assert resp.status_code == 409

    def test_a_home_cannot_have_a_next_guest(
        self, client: TestClient, make_user
    ) -> None:
        """**A checkin on a home is a category error, not a slightly wrong
        value.** Letting it through would put the job on the `same_day` rung of
        a ladder that measures the gap between bookings — and a home has no
        bookings. Refused out loud rather than dropped, because an owner who
        typed a time into a field deserves to know it was ignored.
        """
        owner = make_user(role="owner")
        home = _property(client, owner, property_type="residential")
        resp = _post_job(
            client,
            owner,
            home["id"],
            checkin_at=(datetime.now(timezone.utc) + timedelta(days=5, hours=5)).isoformat(),
        )
        assert resp.status_code == 409
        assert "next guest" in resp.json()["detail"]

    def test_the_rule_has_one_author(self) -> None:
        """The route asks the service; it does not carry its own copy.

        A second place deciding which scope fits which property is how a form
        offers an option the API then refuses.
        """
        rental = Property(property_type=PropertyType.SHORT_TERM_RENTAL)
        home = Property(property_type=PropertyType.RESIDENTIAL)

        assert turnover_rules.service_type_for(rental, None) is ServiceType.TURNOVER
        assert turnover_rules.service_type_for(home, None) is ServiceType.STANDARD
        with pytest.raises(turnover_rules.JobRefused):
            turnover_rules.service_type_for(home, ServiceType.TURNOVER)
        with pytest.raises(turnover_rules.JobRefused):
            turnover_rules.service_type_for(rental, ServiceType.MOVE_OUT)


class TestUrgencyStillHasOneAuthor:
    def test_a_home_job_is_judged_on_how_soon_it_is(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """**No second ladder.** A home job has no checkin, so it takes the
        lead-time branch the urgency rule has always had for a standing
        vacancy — which is the right meaning, not a workaround."""
        owner = make_user(role="owner")
        home = _property(client, owner, property_type="residential")

        soon = _post_job(
            client,
            owner,
            home["id"],
            checkout_at=(datetime.now(timezone.utc) + timedelta(hours=12)).isoformat(),
        )
        later = _post_job(
            client,
            owner,
            home["id"],
            checkout_at=(datetime.now(timezone.utc) + timedelta(days=10)).isoformat(),
        )

        assert soon.json()["urgency"] == TurnoverUrgency.URGENT.value
        assert later.json()["urgency"] == TurnoverUrgency.STANDARD.value

    def test_a_home_job_can_never_be_same_day(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """`same_day` means a guest arrives the day another leaves. There is no
        such thing at a home, and a rung that cannot be reached honestly must
        not be reachable at all."""
        owner = make_user(role="owner")
        home = _property(client, owner, property_type="residential")
        posted = _post_job(
            client,
            owner,
            home["id"],
            checkout_at=(datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
        )
        assert posted.json()["urgency"] != TurnoverUrgency.SAME_DAY.value
        assert posted.json()["is_same_day"] is False


class TestWhatTheCleanerSees:
    def test_the_board_says_what_kind_of_place_and_what_kind_of_clean(
        self, client: TestClient, make_user, make_cleaner, db: Session
    ) -> None:
        """**Both are pricing information.**

        Arriving at somebody's occupied home is a different job from an empty
        rental between guests, and a deep clean is the difference between two
        hours and six. A cleaner who cannot tell before bidding prices one of
        them wrong, and the ones who guess wrong stop bidding.
        """
        owner = make_user(role="owner")
        home = _property(
            client, owner, property_type="residential", square_feet=2200
        )
        _post_job(client, owner, home["id"], service_type="deep")

        cleaner = make_cleaner(cleared=True)
        board = client.get("/api/board", headers=cleaner["auth"])
        assert board.status_code == 200, board.text
        (job,) = [row for row in board.json() if row["property"]["id"] == home["id"]]

        assert job["service_type"] == "deep"
        assert job["property"]["property_type"] == "residential"
        assert job["property"]["square_feet"] == 2200

    def test_the_street_address_is_still_withheld_from_a_home(
        self, client: TestClient, make_user, make_cleaner
    ) -> None:
        """**The privacy boundary does not soften for a home — it matters more.**

        Somebody lives there. A bidding cleaner gets the town and the specs;
        the street address arrives with the award, same as a rental.
        """
        owner = make_user(role="owner")
        home = _property(
            client,
            owner,
            property_type="residential",
            address_line1="77 Private Lane",
            access_notes="Key under the third flowerpot",
        )
        _post_job(client, owner, home["id"])

        cleaner = make_cleaner(cleared=True)
        board = client.get("/api/board", headers=cleaner["auth"])
        assert "77 Private Lane" not in board.text
        assert "flowerpot" not in board.text
        assert "access_notes" not in board.text


class TestTheExistingProductIsUntouched:
    def test_a_rental_still_behaves_exactly_as_before(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """The whole design bet is that residential rides the existing model
        rather than forking it. This is the assertion that the bet held."""
        owner = make_user(role="owner")
        rental = _property(client, owner)

        checkout = datetime.now(timezone.utc) + timedelta(days=2)
        posted = _post_job(
            client,
            owner,
            rental["id"],
            checkout_at=checkout.isoformat(),
            checkin_at=(checkout + timedelta(hours=5)).isoformat(),
        )
        assert posted.status_code == 201, posted.text
        body = posted.json()
        assert body["service_type"] == "turnover"
        assert body["checkin_at"] is not None
        assert body["urgency"] == TurnoverUrgency.SAME_DAY.value
        assert body["is_same_day"] is True

    def test_every_turnover_already_in_the_database_reads_as_a_turnover(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """The server default, checked through the column rather than the API —
        this is what the migration did to every existing row."""
        owner = make_user(role="owner")
        rental = _property(client, owner)
        posted = _post_job(client, owner, rental["id"])

        row = db.get(Turnover, uuid.UUID(posted.json()["id"]))
        assert row.service_type is ServiceType.TURNOVER


# --------------------------------------------------------------------------
# What the review caught after it merged
# --------------------------------------------------------------------------


class TestTheRuleHoldsOnEditToo:
    """**A rule enforced on one path is not a rule.**

    `checkin_for` ran on create and nowhere else, so a home could be given a
    next guest by PATCH. `apply_derived_fields` would then recompute on it and
    the job would reach `same_day` — the rung a home is supposed to have no way
    of reaching — presenting somebody's house as a guest turnover.

    Found by a review bot after the change had already merged, which is the
    argument for the bot: the create path had a test, and the test proved
    exactly as much as the guard it was written against.
    """

    def test_a_home_cannot_be_given_a_checkin_by_patch(
        self, client: TestClient, make_user
    ) -> None:
        owner = make_user(role="owner")
        home = _property(client, owner, property_type="residential")
        job = _post_job(client, owner, home["id"]).json()

        resp = client.patch(
            f"/api/turnovers/{job['id']}",
            json={
                "checkin_at": (
                    datetime.now(timezone.utc) + timedelta(days=5, hours=4)
                ).isoformat()
            },
            headers=owner["auth"],
        )
        assert resp.status_code == 409
        assert "next guest" in resp.json()["detail"]

    def test_the_home_job_is_unchanged_after_the_refusal(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """A refused edit must not half-apply. The row keeps its old schedule
        and, critically, never acquires the same-day flag."""
        owner = make_user(role="owner")
        home = _property(client, owner, property_type="residential")
        job = _post_job(client, owner, home["id"]).json()

        client.patch(
            f"/api/turnovers/{job['id']}",
            json={
                "checkin_at": (
                    datetime.now(timezone.utc) + timedelta(days=5, hours=1)
                ).isoformat()
            },
            headers=owner["auth"],
        )

        db.expire_all()
        row = db.get(Turnover, uuid.UUID(job["id"]))
        assert row.checkin_at is None
        assert row.is_same_day is False
        assert row.urgency is not TurnoverUrgency.SAME_DAY

    def test_a_rental_can_still_be_rescheduled_with_a_checkin(
        self, client: TestClient, make_user
    ) -> None:
        """The guard must not become a blanket refusal — editing a rental's
        schedule is the ordinary case and has to keep working."""
        owner = make_user(role="owner")
        rental = _property(client, owner)
        job = _post_job(client, owner, rental["id"]).json()

        checkin = datetime.now(timezone.utc) + timedelta(days=5, hours=5)
        resp = client.patch(
            f"/api/turnovers/{job['id']}",
            json={"checkin_at": checkin.isoformat()},
            headers=owner["auth"],
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["checkin_at"] is not None


class TestEditingAProperty:
    """**A save that reports success and changes nothing is the worst failure.**

    `PropertyUpdate` declared neither `property_type` nor `square_feet`, while
    the form — which is reused for editing — sent both. Pydantic dropped them
    and the endpoint answered 200 with the old values. An owner who looked up
    their square footage and typed it in was told it was saved.
    """

    def test_square_footage_can_be_added_later(
        self, client: TestClient, make_user
    ) -> None:
        owner = make_user(role="owner")
        created = _property(client, owner)
        assert created["square_feet"] is None

        resp = client.patch(
            f"/api/properties/{created['id']}",
            json={"square_feet": 1450},
            headers=owner["auth"],
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["square_feet"] == 1450, (
            "the endpoint reported success and kept the old value"
        )

    def test_a_property_can_be_reclassified(
        self, client: TestClient, make_user
    ) -> None:
        """**Every property that predates the type column is labelled a rental**,
        whether or not it is one. Without this, they could never be corrected.
        """
        owner = make_user(role="owner")
        created = _property(client, owner)
        assert created["property_type"] == "short_term_rental"

        resp = client.patch(
            f"/api/properties/{created['id']}",
            json={"property_type": "residential"},
            headers=owner["auth"],
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["property_type"] == "residential"

    def test_reclassifying_is_refused_while_work_is_scheduled(
        self, client: TestClient, make_user
    ) -> None:
        """Flipping the type under live jobs would strand them.

        A rental's turnovers carry `service_type=turnover`, which a home is not
        allowed to have — so the jobs would become ones that could never have
        been posted, on a screen asking about guests for a house somebody lives
        in. Refused while anything is live; finish or cancel them first.
        """
        owner = make_user(role="owner")
        rental = _property(client, owner)
        _post_job(client, owner, rental["id"])

        resp = client.patch(
            f"/api/properties/{rental['id']}",
            json={"property_type": "residential"},
            headers=owner["auth"],
        )
        assert resp.status_code == 409
        assert "still scheduled" in resp.json()["detail"]

    def test_an_unrelated_edit_is_unaffected_by_the_guard(
        self, client: TestClient, make_user
    ) -> None:
        """The guard is about *changing* the type. Renaming a property with
        live jobs must still work."""
        owner = make_user(role="owner")
        rental = _property(client, owner)
        _post_job(client, owner, rental["id"])

        resp = client.patch(
            f"/api/properties/{rental['id']}",
            json={"nickname": "The Cottage"},
            headers=owner["auth"],
        )
        assert resp.status_code == 200, resp.text

    def test_resending_the_same_type_is_not_a_change(
        self, client: TestClient, make_user
    ) -> None:
        """The form submits every field. Sending the type it already has must
        not trip a guard about changing it."""
        owner = make_user(role="owner")
        rental = _property(client, owner)
        _post_job(client, owner, rental["id"])

        resp = client.patch(
            f"/api/properties/{rental['id']}",
            json={"property_type": "short_term_rental", "nickname": "Same type"},
            headers=owner["auth"],
        )
        assert resp.status_code == 200, resp.text


class TestTheEmailReadsRight:
    def test_a_home_is_not_described_as_a_turnover(
        self, client: TestClient, make_user, make_cleaner, db: Session
    ) -> None:
        """**The fields are not merely empty — they are the wrong question.**

        A cleaner reading "next checkin: none booked yet" about somebody's
        house is being told it is an empty rental between guests.
        """
        from app.models.notification import Notification
        from app.models.enums import NotificationEvent
        from sqlalchemy import select

        make_cleaner(cleared=True)
        owner = make_user(role="owner")
        home = _property(client, owner, property_type="residential")
        _post_job(client, owner, home["id"], service_type="deep")

        posted = db.execute(
            select(Notification).where(
                Notification.event == NotificationEvent.TURNOVER_POSTED
            )
        ).scalars().all()
        assert posted, "nobody was told about a job in their area"

        body = posted[0].body
        assert "checkin" not in body.lower()
        assert "Scheduled for:" in body
        assert "deep clean" in body
        assert "turnover" not in posted[0].subject.lower()

    def test_a_rental_still_reads_as_a_turnover(
        self, client: TestClient, make_user, make_cleaner, db: Session
    ) -> None:
        from app.models.notification import Notification
        from app.models.enums import NotificationEvent
        from sqlalchemy import select

        make_cleaner(cleared=True)
        owner = make_user(role="owner")
        rental = _property(client, owner)
        _post_job(client, owner, rental["id"])

        posted = db.execute(
            select(Notification).where(
                Notification.event == NotificationEvent.TURNOVER_POSTED
            )
        ).scalars().all()
        body = posted[0].body
        assert "Checkout:" in body
        assert "checkin" in body.lower()
        assert "turnover" in posted[0].subject.lower()


class TestTheSecondReviewRound:
    """Four more findings, on the fixes for the first three.

    Worth recording as a pattern rather than a list: each round of fixes had
    its own seams, and the bot found them in the same places a person does —
    the path that was not guarded, the null that was not considered, the
    history that changes meaning underneath somebody.
    """

    def test_an_explicit_null_is_a_422_not_a_500(
        self, client: TestClient, make_user
    ) -> None:
        """**Omission and erasure are different requests.**

        Every field on a PATCH shape is optional so that leaving it out means
        "keep it" — but the same `| None` makes an *explicit* null look valid,
        `exclude_unset` keeps it, and Postgres rejects it on a non-nullable
        column as a 500. This predates the residential work on `bedrooms` and
        the rest; `property_type` just joined them.
        """
        owner = make_user(role="owner")
        created = _property(client, owner)

        for field in ("property_type", "bedrooms", "nickname", "is_active"):
            resp = client.patch(
                f"/api/properties/{created['id']}",
                json={field: None},
                headers=owner["auth"],
            )
            assert resp.status_code == 422, f"{field} null returned {resp.status_code}"

    def test_a_nullable_column_can_still_be_cleared(
        self, client: TestClient, make_user
    ) -> None:
        """The guard must not become a blanket ban. `square_feet` *is*
        nullable, so an owner who guessed wrong can take the number back out."""
        owner = make_user(role="owner")
        created = _property(client, owner, square_feet=1500)

        resp = client.patch(
            f"/api/properties/{created['id']}",
            json={"square_feet": None},
            headers=owner["auth"],
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["square_feet"] is None

    def test_reclassification_and_job_creation_cannot_interleave(
        self,
        client: TestClient,
        make_user,
        db: Session,
        own_session_per_request,
    ) -> None:
        """**Both requests could read the old type.**

        The guard counted live jobs without a lock, so a PATCH could see zero
        while a POST was committing a job for the type about to change —
        producing exactly the incompatible live job the guard exists to
        prevent. Not guardrail 1 (nothing indivisible is handed out) but the
        same check-then-write shape, so it gets the same answer: both paths
        lock the property row.

        Racing them, either order is acceptable — what is not acceptable is
        both succeeding.
        """
        import threading

        from app.main import app

        owner = make_user(role="owner")
        rental = _property(client, owner)
        db.commit()

        start = threading.Barrier(2)
        results: dict[str, int] = {}

        def reclassify() -> None:
            with TestClient(app) as racer:
                start.wait(timeout=10)
                results["patch"] = racer.patch(
                    f"/api/properties/{rental['id']}",
                    json={"property_type": "residential"},
                    headers=owner["auth"],
                ).status_code

        def post_job() -> None:
            with TestClient(app) as racer:
                start.wait(timeout=10)
                results["post"] = racer.post(
                    "/api/turnovers",
                    json={
                        "property_id": rental["id"],
                        "checkout_at": (
                            datetime.now(timezone.utc) + timedelta(days=5)
                        ).isoformat(),
                    },
                    headers=owner["auth"],
                ).status_code

        threads = [threading.Thread(target=reclassify), threading.Thread(target=post_job)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            assert not thread.is_alive(), "a request never returned — deadlock?"

        db.expire_all()
        prop = db.get(Property, uuid.UUID(rental["id"]))
        jobs = db.execute(
            select(Turnover).where(Turnover.property_id == prop.id)
        ).scalars().all()

        # Whatever order they landed in, the property and its live jobs agree.
        for job in jobs:
            if job.status in (TurnoverStatus.DRAFT, TurnoverStatus.OPEN):
                allowed = turnover_rules.SERVICE_TYPES_FOR[prop.property_type]
                assert job.service_type in allowed, (
                    f"a live {job.service_type.value} job is on a "
                    f"{prop.property_type.value} property — the two requests "
                    "interleaved and both read the old type"
                )

    def test_the_property_lock_is_real(
        self,
        client: TestClient,
        make_user,
        db: Session,
        own_session_per_request,
    ) -> None:
        """**Evidence the lock is taken, not just that a race did not happen.**

        The test above passes against an implementation with no locks at all on
        most runs, because two threads rarely interleave inside the window that
        matters. CLAUDE.md says exactly this about the award race and keeps a
        second, deterministic test for it; this is that test for this path.

        The competing connection holds **`FOR NO KEY UPDATE`** — precisely what
        a plain `UPDATE properties SET property_type = ...` takes — rather than
        the stronger `FOR UPDATE`. That distinction is the whole test, and the
        first draft of it got it wrong:

        Inserting a turnover takes `FOR KEY SHARE` on the property it
        references, for the foreign key. `FOR UPDATE` conflicts with that, so a
        holder taking `FOR UPDATE` blocks the insert whether or not the route
        locks anything — the test passed against an implementation with no lock
        at all, which is the same failure mode it was written to catch, one
        level up.

        `FOR NO KEY UPDATE` does not conflict with `FOR KEY SHARE`. So the
        insert proceeds unless the route asks for the lock itself, and this
        blocks only if the lock is real.
        """
        import threading

        from app.db import SessionLocal
        from app.main import app

        owner = make_user(role="owner")
        rental = _property(client, owner)
        property_id = uuid.UUID(rental["id"])
        auth = owner["auth"]
        db.commit()

        holder = SessionLocal()
        finished = threading.Event()
        outcome: dict[str, int] = {}

        try:
            holder.execute(
                select(Property)
                .where(Property.id == property_id)
                # key_share=True renders FOR NO KEY UPDATE — see the docstring.
                .with_for_update(key_share=True)
            ).scalar_one()

            def post_job() -> None:
                with TestClient(app) as racer:
                    outcome["status"] = racer.post(
                        "/api/turnovers",
                        json={
                            "property_id": str(property_id),
                            "checkout_at": (
                                datetime.now(timezone.utc) + timedelta(days=5)
                            ).isoformat(),
                        },
                        headers=auth,
                    ).status_code
                finished.set()

            thread = threading.Thread(target=post_job)
            thread.start()

            assert not finished.wait(timeout=2.0), (
                "the job was created while another connection held the "
                "property's row lock — so its type was read without one, and "
                "a reclassification running alongside would strand the job"
            )

            holder.rollback()  # releases the lock

            assert finished.wait(timeout=30), "the request never returned after the lock lifted"
            thread.join(timeout=5)
            assert outcome["status"] == 201
        finally:
            holder.rollback()
            holder.close()
