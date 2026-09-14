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
from sqlalchemy.orm import Session

from app.models.enums import PropertyType, ServiceType, TurnoverUrgency
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
