"""The bench board and bidding.

Two things matter most here and are tested first: what a cleaner can *see*
about a job they have not been hired for, and whether an un-vetted cleaner can
bid. Everything else is ordering and bookkeeping.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

PORTLAND_ME = (43.6591, -70.2568)
OLD_ORCHARD_BEACH = (43.5148, -70.3778)  # ~12 mi from Portland
BRUNSWICK_ME = (43.9145, -69.9653)  # ~23 mi
BOSTON_MA = (42.3601, -71.0589)  # ~98 mi


class TestWhatACleanerMaySee:
    """The privacy boundary of the product.

    A cleaner browsing the board has not been hired. They get what they need to
    price the job and nothing that would let them turn up at the door.
    """

    def test_the_board_withholds_the_address_and_the_gate_code(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        cleaner = make_cleaner(cleared=True)
        make_open_turnover()

        board = client.get("/api/board", headers=cleaner["auth"]).json()
        assert len(board) == 1
        listing = board[0]

        # The whole response, flattened, must not contain either.
        blob = repr(listing)
        assert "4417" not in blob, "the lockbox code reached the board"
        assert "Harbor Way" not in blob, "the street address reached the board"

        assert "access_notes" not in listing["property"]
        assert "address_line1" not in listing["property"]
        assert "address_line2" not in listing["property"]
        assert "owner_id" not in listing["property"]

    def test_but_it_gives_enough_to_price_the_job(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        cleaner = make_cleaner(cleared=True)
        make_open_turnover()

        listing = client.get("/api/board", headers=cleaner["auth"]).json()[0]
        assert listing["property"]["city"] == "Portland"
        assert listing["property"]["bedrooms"] == 2
        assert listing["property"]["cleaning_notes"] == "Linens in the hall closet."
        assert listing["owner_budget_cents"] == 14_500
        assert listing["urgency"]
        assert listing["distance_miles"] == pytest.approx(0, abs=0.2)

    def test_the_detail_view_withholds_the_same_things(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        """A second shape means a second chance to leak. Both are checked."""
        cleaner = make_cleaner(cleared=True)
        scenario = make_open_turnover()

        listing = client.get(
            f"/api/board/{scenario['turnover']['id']}", headers=cleaner["auth"]
        ).json()
        blob = repr(listing)
        assert "4417" not in blob
        assert "Harbor Way" not in blob

    def test_an_owner_cannot_browse_the_board(
        self, client: TestClient, make_user
    ) -> None:
        owner = make_user(role="owner")
        assert client.get("/api/board", headers=owner["auth"]).status_code == 403


class TestTheBiddingGate:
    def test_an_unvetted_cleaner_can_look_but_not_bid(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        """Seeing the work is how someone decides vetting is worth finishing."""
        cleaner = make_cleaner(cleared=False)
        scenario = make_open_turnover()

        assert len(client.get("/api/board", headers=cleaner["auth"]).json()) == 1

        resp = client.put(
            f"/api/board/{scenario['turnover']['id']}/bid",
            json={"price_cents": 12_000},
            headers=cleaner["auth"],
        )
        assert resp.status_code == 403
        # And it says why, in the same words the profile badge uses.
        detail = resp.json()["detail"]
        assert "ID verification not started" in detail
        assert "background check not started" in detail

    def test_the_refusal_matches_what_the_profile_says(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        """The gate and the badge read the same function, so they cannot disagree.

        This is the trap the vetting service exists to prevent: a screen saying
        "verified" beside a button that silently refuses.
        """
        cleaner = make_cleaner(cleared=False)
        scenario = make_open_turnover()

        profile_summary = client.get("/api/cleaner/profile", headers=cleaner["auth"]).json()[
            "vetting"
        ]["summary"]
        refusal = client.put(
            f"/api/board/{scenario['turnover']['id']}/bid",
            json={"price_cents": 12_000},
            headers=cleaner["auth"],
        ).json()["detail"]

        assert refusal == profile_summary

    def test_half_vetted_is_still_not_vetted(
        self, client: TestClient, make_cleaner, make_open_turnover, admin_user
    ) -> None:
        cleaner = make_cleaner(cleared=False)
        scenario = make_open_turnover()

        client.post(
            f"/api/admin/cleaners/{cleaner['profile_id']}/id-verification",
            json={"status": "approved"},
            headers=admin_user["auth"],
        )

        resp = client.put(
            f"/api/board/{scenario['turnover']['id']}/bid",
            json={"price_cents": 12_000},
            headers=cleaner["auth"],
        )
        assert resp.status_code == 403
        assert "background check" in resp.json()["detail"]

    def test_a_cleared_cleaner_can_bid(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        cleaner = make_cleaner(cleared=True)
        scenario = make_open_turnover()

        resp = client.put(
            f"/api/board/{scenario['turnover']['id']}/bid",
            json={"price_cents": 12_500, "message": "I can do it that morning."},
            headers=cleaner["auth"],
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["price_cents"] == 12_500
        assert body["status"] == "submitted"

    def test_missing_insurance_does_not_block_bidding(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        """A flag, not a gate — requiring a COI while supply is scarce kills launch."""
        cleaner = make_cleaner(cleared=True, has_insurance=False)
        scenario = make_open_turnover()

        resp = client.put(
            f"/api/board/{scenario['turnover']['id']}/bid",
            json={"price_cents": 12_500},
            headers=cleaner["auth"],
        )
        assert resp.status_code == 200


class TestTheRadius:
    def test_jobs_outside_the_radius_are_not_shown(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        cleaner = make_cleaner(cleared=True, radius_miles=15)
        make_open_turnover(lat=OLD_ORCHARD_BEACH[0], lng=OLD_ORCHARD_BEACH[1], nickname="Near")
        make_open_turnover(lat=BOSTON_MA[0], lng=BOSTON_MA[1], nickname="Far")

        board = client.get("/api/board", headers=cleaner["auth"]).json()
        assert [listing["property"]["nickname"] for listing in board] == ["Near"]

    def test_a_wider_radius_reaches_further(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        cleaner = make_cleaner(cleared=True, radius_miles=30)
        make_open_turnover(lat=OLD_ORCHARD_BEACH[0], lng=OLD_ORCHARD_BEACH[1], nickname="Near")
        make_open_turnover(lat=BRUNSWICK_ME[0], lng=BRUNSWICK_ME[1], nickname="Mid")
        make_open_turnover(lat=BOSTON_MA[0], lng=BOSTON_MA[1], nickname="Far")

        board = client.get("/api/board", headers=cleaner["auth"]).json()
        assert sorted(listing["property"]["nickname"] for listing in board) == ["Mid", "Near"]

    def test_the_reported_distance_is_right(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        cleaner = make_cleaner(cleared=True, radius_miles=50)
        make_open_turnover(lat=BRUNSWICK_ME[0], lng=BRUNSWICK_ME[1])

        listing = client.get("/api/board", headers=cleaner["auth"]).json()[0]
        assert listing["distance_miles"] == pytest.approx(22.9, abs=0.2)

    def test_a_cleaner_with_no_service_area_sees_an_empty_board(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        """Better an honest empty board than turnovers from an invented location."""
        cleaner = make_cleaner(cleared=True, lat=None, lng=None)
        make_open_turnover()
        assert client.get("/api/board", headers=cleaner["auth"]).json() == []

    def test_a_property_without_coordinates_is_not_shown(
        self, client: TestClient, make_cleaner, make_user
    ) -> None:
        """It cannot be placed in a radius, so it must not be silently included."""
        cleaner = make_cleaner(cleared=True)
        owner = make_user(role="owner")
        prop = client.post(
            "/api/properties",
            json={
                "nickname": "Unlocated",
                "address_line1": "9 Nowhere St",
                "city": "Portland",
                "state": "ME",
                "postal_code": "04101",
            },
            headers=owner["auth"],
        ).json()
        client.post(
            "/api/turnovers",
            json={
                "property_id": prop["id"],
                "checkout_at": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat(),
            },
            headers=owner["auth"],
        )

        assert client.get("/api/board", headers=cleaner["auth"]).json() == []


class TestWhatAppearsOnTheBoard:
    def test_only_open_turnovers(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        cleaner = make_cleaner(cleared=True)
        make_open_turnover(nickname="Open")
        drafted = make_open_turnover(nickname="Draft")

        client.post(
            f"/api/turnovers/{drafted['turnover']['id']}/cancel",
            json={},
            headers=drafted["owner"]["auth"],
        )

        board = client.get("/api/board", headers=cleaner["auth"]).json()
        assert [listing["property"]["nickname"] for listing in board] == ["Open"]

    def test_a_draft_is_invisible_until_it_is_posted(
        self, client: TestClient, make_cleaner, make_user
    ) -> None:
        cleaner = make_cleaner(cleared=True)
        owner = make_user(role="owner")
        prop = client.post(
            "/api/properties",
            json={
                "nickname": "Quiet",
                "address_line1": "3 Harbor Way",
                "city": "Portland",
                "state": "ME",
                "postal_code": "04101",
                "lat": str(PORTLAND_ME[0]),
                "lng": str(PORTLAND_ME[1]),
            },
            headers=owner["auth"],
        ).json()
        turnover = client.post(
            "/api/turnovers",
            json={
                "property_id": prop["id"],
                "checkout_at": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat(),
                "publish": False,
            },
            headers=owner["auth"],
        ).json()

        assert client.get("/api/board", headers=cleaner["auth"]).json() == []

        client.post(f"/api/turnovers/{turnover['id']}/publish", headers=owner["auth"])
        assert len(client.get("/api/board", headers=cleaner["auth"]).json()) == 1

    def test_past_checkouts_are_hidden_unless_asked_for(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        cleaner = make_cleaner(cleared=True)
        make_open_turnover(days_out=-2, checkin_hours_after=None, nickname="Gone")

        assert client.get("/api/board", headers=cleaner["auth"]).json() == []

        with_past = client.get(
            "/api/board", params={"include_past": True}, headers=cleaner["auth"]
        ).json()
        assert [listing["property"]["nickname"] for listing in with_past] == ["Gone"]

    def test_the_most_urgent_job_is_first(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        """The order a cleaner filling a day actually wants."""
        cleaner = make_cleaner(cleared=True, radius_miles=50)
        make_open_turnover(days_out=20, checkin_hours_after=240, nickname="Standard")
        make_open_turnover(days_out=10, checkin_hours_after=2, nickname="Same day")
        make_open_turnover(days_out=15, checkin_hours_after=48, nickname="Soon")

        board = client.get("/api/board", headers=cleaner["auth"]).json()
        assert [listing["property"]["nickname"] for listing in board] == [
            "Same day",
            "Soon",
            "Standard",
        ]


class TestBids:
    def test_bidding_twice_replaces_the_price(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        """One bid per cleaner per turnover — the owner never sees a duplicate."""
        cleaner = make_cleaner(cleared=True)
        scenario = make_open_turnover()
        url = f"/api/board/{scenario['turnover']['id']}/bid"

        first = client.put(url, json={"price_cents": 12_500}, headers=cleaner["auth"]).json()
        second = client.put(url, json={"price_cents": 11_000}, headers=cleaner["auth"]).json()

        assert second["id"] == first["id"]
        assert second["price_cents"] == 11_000
        assert len(client.get("/api/board/bids/mine", headers=cleaner["auth"]).json()) == 1

    def test_the_board_shows_a_cleaner_their_own_bid(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        cleaner = make_cleaner(cleared=True)
        scenario = make_open_turnover()
        client.put(
            f"/api/board/{scenario['turnover']['id']}/bid",
            json={"price_cents": 12_500},
            headers=cleaner["auth"],
        )

        listing = client.get("/api/board", headers=cleaner["auth"]).json()[0]
        assert listing["my_bid"]["price_cents"] == 12_500

    def test_a_cleaner_never_sees_another_cleaners_price(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        first = make_cleaner(cleared=True)
        second = make_cleaner(cleared=True)
        scenario = make_open_turnover()

        client.put(
            f"/api/board/{scenario['turnover']['id']}/bid",
            json={"price_cents": 9_900},
            headers=first["auth"],
        )

        listing = client.get("/api/board", headers=second["auth"]).json()[0]
        assert listing["my_bid"] is None
        assert "9900" not in repr(listing)

    def test_a_zero_or_negative_price_is_refused(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        cleaner = make_cleaner(cleared=True)
        scenario = make_open_turnover()
        url = f"/api/board/{scenario['turnover']['id']}/bid"

        assert client.put(url, json={"price_cents": 0}, headers=cleaner["auth"]).status_code == 422
        assert (
            client.put(url, json={"price_cents": -500}, headers=cleaner["auth"]).status_code
            == 422
        )

    def test_you_cannot_bid_on_a_cancelled_turnover(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        cleaner = make_cleaner(cleared=True)
        scenario = make_open_turnover()
        client.post(
            f"/api/turnovers/{scenario['turnover']['id']}/cancel",
            json={},
            headers=scenario["owner"]["auth"],
        )

        resp = client.put(
            f"/api/board/{scenario['turnover']['id']}/bid",
            json={"price_cents": 12_000},
            headers=cleaner["auth"],
        )
        assert resp.status_code == 409

    def test_withdrawing_marks_the_bid_rather_than_deleting_it(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        """A silently vanishing bid tells the owner nothing; a withdrawal does."""
        cleaner = make_cleaner(cleared=True)
        scenario = make_open_turnover()
        url = f"/api/board/{scenario['turnover']['id']}/bid"
        client.put(url, json={"price_cents": 12_500}, headers=cleaner["auth"])

        resp = client.delete(url, headers=cleaner["auth"])
        assert resp.status_code == 200
        assert resp.json()["status"] == "withdrawn"

        mine = client.get("/api/board/bids/mine", headers=cleaner["auth"]).json()
        assert [bid["status"] for bid in mine] == ["withdrawn"]

    def test_re_bidding_after_a_withdrawal_puts_it_back_in_front_of_the_owner(
        self, client: TestClient, make_cleaner, make_open_turnover
    ) -> None:
        cleaner = make_cleaner(cleared=True)
        scenario = make_open_turnover()
        url = f"/api/board/{scenario['turnover']['id']}/bid"

        client.put(url, json={"price_cents": 12_500}, headers=cleaner["auth"])
        client.delete(url, headers=cleaner["auth"])

        again = client.put(url, json={"price_cents": 10_000}, headers=cleaner["auth"]).json()
        assert again["status"] == "submitted"

    def test_an_accepted_bid_cannot_be_changed_or_withdrawn(
        self, client: TestClient, make_cleaner, make_open_turnover, db
    ) -> None:
        """Awarding is phase 4; this only guards the door from the cleaner's side."""
        import uuid as _uuid

        from app.models import Bid, BidStatus

        cleaner = make_cleaner(cleared=True)
        scenario = make_open_turnover()
        url = f"/api/board/{scenario['turnover']['id']}/bid"
        bid_id = client.put(url, json={"price_cents": 12_500}, headers=cleaner["auth"]).json()[
            "id"
        ]

        row = db.get(Bid, _uuid.UUID(bid_id))
        row.status = BidStatus.ACCEPTED
        db.commit()

        assert client.put(url, json={"price_cents": 9_000}, headers=cleaner["auth"]).status_code == 409
        assert client.delete(url, headers=cleaner["auth"]).status_code == 409
