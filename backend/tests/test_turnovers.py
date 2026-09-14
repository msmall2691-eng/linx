"""Owner-facing turnover endpoints: creation, the derived ladder, and scoping."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

PROPERTY = {
    "nickname": "Seaside Cottage",
    "address_line1": "1 Harbor Way",
    "city": "Portland",
    "state": "ME",
    "postal_code": "04101",
    "bedrooms": 2,
    "bathrooms": "1.5",
    "access_notes": "Lockbox on the rail, code 4417.",
}


def _property(client: TestClient, auth: dict, **overrides) -> dict:
    resp = client.post("/api/properties", json={**PROPERTY, **overrides}, headers=auth)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _turnover(client: TestClient, auth: dict, property_id: str, **overrides) -> dict:
    checkout = datetime.now(timezone.utc) + timedelta(days=7)
    payload = {
        "property_id": property_id,
        "checkout_at": checkout.isoformat(),
        "checkin_at": (checkout + timedelta(days=4)).isoformat(),
        **overrides,
    }
    resp = client.post("/api/turnovers", json=payload, headers=auth)
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestCreate:
    def test_an_owner_can_post_a_turnover(self, client: TestClient, make_user) -> None:
        owner = make_user(role="owner")
        prop = _property(client, owner["auth"])
        body = _turnover(client, owner["auth"], prop["id"], notes="Linens in the hall closet.")

        assert body["property_id"] == prop["id"]
        assert body["status"] == "open"
        assert body["notes"] == "Linens in the hall closet."

    def test_it_can_be_kept_as_a_draft(self, client: TestClient, make_user) -> None:
        owner = make_user(role="owner")
        prop = _property(client, owner["auth"])
        body = _turnover(client, owner["auth"], prop["id"], publish=False)
        assert body["status"] == "draft"

    def test_a_standing_vacancy_needs_no_checkin(self, client: TestClient, make_user) -> None:
        owner = make_user(role="owner")
        prop = _property(client, owner["auth"])
        checkout = datetime.now(timezone.utc) + timedelta(days=10)

        resp = client.post(
            "/api/turnovers",
            json={"property_id": prop["id"], "checkout_at": checkout.isoformat()},
            headers=owner["auth"],
        )
        assert resp.status_code == 201
        assert resp.json()["checkin_at"] is None
        assert resp.json()["is_same_day"] is False

    def test_a_naive_timestamp_is_refused_rather_than_guessed(
        self, client: TestClient, make_user
    ) -> None:
        """Reading a naive time as UTC moves a Maine checkout by four hours."""
        owner = make_user(role="owner")
        prop = _property(client, owner["auth"])

        resp = client.post(
            "/api/turnovers",
            json={"property_id": prop["id"], "checkout_at": "2026-07-01T11:00:00"},
            headers=owner["auth"],
        )
        assert resp.status_code == 422

    def test_a_checkin_before_checkout_is_refused(self, client: TestClient, make_user) -> None:
        owner = make_user(role="owner")
        prop = _property(client, owner["auth"])
        checkout = datetime.now(timezone.utc) + timedelta(days=5)

        resp = client.post(
            "/api/turnovers",
            json={
                "property_id": prop["id"],
                "checkout_at": checkout.isoformat(),
                "checkin_at": (checkout - timedelta(hours=2)).isoformat(),
            },
            headers=owner["auth"],
        )
        assert resp.status_code == 422

    def test_you_cannot_post_against_someone_elses_property(
        self, client: TestClient, make_user
    ) -> None:
        first = make_user(role="owner")
        second = make_user(role="owner")
        theirs = _property(client, second["auth"])
        checkout = datetime.now(timezone.utc) + timedelta(days=5)

        resp = client.post(
            "/api/turnovers",
            json={"property_id": theirs["id"], "checkout_at": checkout.isoformat()},
            headers=first["auth"],
        )
        assert resp.status_code == 404

    def test_an_archived_property_cannot_take_new_turnovers(
        self, client: TestClient, make_user
    ) -> None:
        owner = make_user(role="owner")
        prop = _property(client, owner["auth"])
        client.delete(f"/api/properties/{prop['id']}", headers=owner["auth"])

        checkout = datetime.now(timezone.utc) + timedelta(days=5)
        resp = client.post(
            "/api/turnovers",
            json={"property_id": prop["id"], "checkout_at": checkout.isoformat()},
            headers=owner["auth"],
        )
        assert resp.status_code == 409

    def test_a_cleaner_cannot_post_a_turnover(self, client: TestClient, make_user) -> None:
        owner = make_user(role="owner")
        cleaner = make_user(role="cleaner")
        prop = _property(client, owner["auth"])
        checkout = datetime.now(timezone.utc) + timedelta(days=5)

        resp = client.post(
            "/api/turnovers",
            json={"property_id": prop["id"], "checkout_at": checkout.isoformat()},
            headers=cleaner["auth"],
        )
        assert resp.status_code == 403


class TestUrgencyIsDerivedNotSupplied:
    def test_a_wide_window_comes_back_standard(self, client: TestClient, make_user) -> None:
        owner = make_user(role="owner")
        prop = _property(client, owner["auth"])
        body = _turnover(client, owner["auth"], prop["id"])
        assert body["urgency"] == "standard"

    def test_a_same_day_turnaround_is_flagged_on_the_way_in(
        self, client: TestClient, make_user
    ) -> None:
        owner = make_user(role="owner")
        prop = _property(client, owner["auth"])
        checkout = datetime(2026, 7, 1, 15, 0, tzinfo=timezone.utc)  # 11am Eastern

        body = _turnover(
            client,
            owner["auth"],
            prop["id"],
            checkout_at=checkout.isoformat(),
            checkin_at=(checkout + timedelta(hours=5)).isoformat(),
        )
        assert body["urgency"] == "same_day"
        assert body["is_same_day"] is True

    def test_a_client_supplied_urgency_is_ignored(self, client: TestClient, make_user) -> None:
        """The ladder has one author. A request is not it."""
        owner = make_user(role="owner")
        prop = _property(client, owner["auth"])
        checkout = datetime.now(timezone.utc) + timedelta(days=30)

        resp = client.post(
            "/api/turnovers",
            json={
                "property_id": prop["id"],
                "checkout_at": checkout.isoformat(),
                "checkin_at": (checkout + timedelta(days=5)).isoformat(),
                "urgency": "same_day",
                "is_same_day": True,
                "status": "awarded",
            },
            headers=owner["auth"],
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["urgency"] == "standard"
        assert body["is_same_day"] is False
        assert body["status"] == "open"

    def test_rescheduling_recomputes_the_ladder(self, client: TestClient, make_user) -> None:
        owner = make_user(role="owner")
        prop = _property(client, owner["auth"])
        created = _turnover(client, owner["auth"], prop["id"])
        assert created["urgency"] == "standard"

        checkout = datetime(2026, 7, 1, 15, 0, tzinfo=timezone.utc)
        resp = client.patch(
            f"/api/turnovers/{created['id']}",
            json={
                "checkout_at": checkout.isoformat(),
                "checkin_at": (checkout + timedelta(hours=4)).isoformat(),
            },
            headers=owner["auth"],
        )
        assert resp.status_code == 200
        assert resp.json()["urgency"] == "same_day"

    def test_clearing_the_checkin_turns_it_into_a_vacancy(
        self, client: TestClient, make_user
    ) -> None:
        """`checkin_at: null` in a PATCH is ambiguous, so clearing is explicit."""
        owner = make_user(role="owner")
        prop = _property(client, owner["auth"])
        checkout = datetime.now(timezone.utc) + timedelta(hours=12)
        created = _turnover(
            client,
            owner["auth"],
            prop["id"],
            checkout_at=checkout.isoformat(),
            checkin_at=(checkout + timedelta(days=6)).isoformat(),
        )
        assert created["urgency"] == "standard"

        resp = client.patch(
            f"/api/turnovers/{created['id']}",
            json={"clear_checkin": True},
            headers=owner["auth"],
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["checkin_at"] is None
        # Checkout is twelve hours out, so as a vacancy it is now urgent.
        assert body["urgency"] == "urgent"

    def test_a_stale_vacancy_is_refreshed_when_it_is_read(
        self, client: TestClient, make_user, db
    ) -> None:
        """A vacancy climbs the ladder as checkout approaches.

        Urgency is stored so the board can sort on it, which means it can go
        stale. The read path recomputes rather than serving a value the same
        rule would no longer produce.
        """
        import uuid as _uuid

        from app.models import Turnover, TurnoverUrgency

        owner = make_user(role="owner")
        prop = _property(client, owner["auth"])
        checkout = datetime.now(timezone.utc) + timedelta(days=10)
        created = client.post(
            "/api/turnovers",
            json={"property_id": prop["id"], "checkout_at": checkout.isoformat()},
            headers=owner["auth"],
        ).json()
        assert created["urgency"] == "standard"

        # Move checkout to this afternoon behind the endpoint's back, the way
        # the passage of time would.
        row = db.get(Turnover, _uuid.UUID(created["id"]))
        row.checkout_at = datetime.now(timezone.utc) + timedelta(hours=3)
        db.commit()
        assert row.urgency is TurnoverUrgency.STANDARD  # still the stale value

        fetched = client.get(f"/api/turnovers/{created['id']}", headers=owner["auth"]).json()
        assert fetched["urgency"] == "urgent"

        db.refresh(row)
        assert row.urgency is TurnoverUrgency.URGENT  # and it was persisted


class TestListAndDetail:
    def test_the_list_shows_only_your_own(self, client: TestClient, make_user) -> None:
        first = make_user(role="owner")
        second = make_user(role="owner")
        _turnover(client, first["auth"], _property(client, first["auth"])["id"])
        _turnover(client, second["auth"], _property(client, second["auth"])["id"])

        body = client.get("/api/turnovers", headers=first["auth"]).json()
        assert len(body) == 1

    def test_another_owners_turnover_reads_as_missing(
        self, client: TestClient, make_user
    ) -> None:
        first = make_user(role="owner")
        second = make_user(role="owner")
        theirs = _turnover(client, second["auth"], _property(client, second["auth"])["id"])

        resp = client.get(f"/api/turnovers/{theirs['id']}", headers=first["auth"])
        assert resp.status_code == 404

    def test_the_list_is_ordered_by_soonest_checkout(
        self, client: TestClient, make_user
    ) -> None:
        owner = make_user(role="owner")
        prop = _property(client, owner["auth"])
        now = datetime.now(timezone.utc)

        for days in (20, 3, 9):
            client.post(
                "/api/turnovers",
                json={
                    "property_id": prop["id"],
                    "checkout_at": (now + timedelta(days=days)).isoformat(),
                },
                headers=owner["auth"],
            )

        body = client.get("/api/turnovers", headers=owner["auth"]).json()
        checkouts = [t["checkout_at"] for t in body]
        assert checkouts == sorted(checkouts)

    def test_finished_turnovers_are_hidden_unless_asked_for(
        self, client: TestClient, make_user
    ) -> None:
        owner = make_user(role="owner")
        prop = _property(client, owner["auth"])
        live = _turnover(client, owner["auth"], prop["id"])
        gone = _turnover(
            client,
            owner["auth"],
            prop["id"],
            checkout_at=(datetime.now(timezone.utc) + timedelta(days=14)).isoformat(),
            checkin_at=None,
        )
        client.post(f"/api/turnovers/{gone['id']}/cancel", json={}, headers=owner["auth"])

        default = client.get("/api/turnovers", headers=owner["auth"]).json()
        assert [t["id"] for t in default] == [live["id"]]

        everything = client.get(
            "/api/turnovers", params={"include_finished": True}, headers=owner["auth"]
        ).json()
        assert len(everything) == 2

    def test_it_can_be_filtered_by_status_and_property(
        self, client: TestClient, make_user
    ) -> None:
        owner = make_user(role="owner")
        first = _property(client, owner["auth"], nickname="First")
        second = _property(client, owner["auth"], nickname="Second")
        _turnover(client, owner["auth"], first["id"])
        draft = _turnover(client, owner["auth"], second["id"], publish=False)

        by_status = client.get(
            "/api/turnovers", params={"status": "draft"}, headers=owner["auth"]
        ).json()
        assert [t["id"] for t in by_status] == [draft["id"]]

        by_property = client.get(
            "/api/turnovers", params={"property_id": second["id"]}, headers=owner["auth"]
        ).json()
        assert [t["id"] for t in by_property] == [draft["id"]]

    def test_the_detail_view_carries_the_property(self, client: TestClient, make_user) -> None:
        """One request per screen, access notes included — this is the owner."""
        owner = make_user(role="owner")
        prop = _property(client, owner["auth"])
        created = _turnover(client, owner["auth"], prop["id"])

        body = client.get(f"/api/turnovers/{created['id']}", headers=owner["auth"]).json()
        assert body["property"]["id"] == prop["id"]
        assert body["property"]["access_notes"] == PROPERTY["access_notes"]


class TestStatusTransitions:
    def test_publishing_moves_a_draft_onto_the_bench(
        self, client: TestClient, make_user
    ) -> None:
        owner = make_user(role="owner")
        prop = _property(client, owner["auth"])
        draft = _turnover(client, owner["auth"], prop["id"], publish=False)

        resp = client.post(f"/api/turnovers/{draft['id']}/publish", headers=owner["auth"])
        assert resp.status_code == 200
        assert resp.json()["status"] == "open"

    def test_publishing_twice_is_harmless(self, client: TestClient, make_user) -> None:
        owner = make_user(role="owner")
        prop = _property(client, owner["auth"])
        created = _turnover(client, owner["auth"], prop["id"])

        resp = client.post(f"/api/turnovers/{created['id']}/publish", headers=owner["auth"])
        assert resp.status_code == 200
        assert resp.json()["status"] == "open"

    def test_cancelling_records_when_and_why(self, client: TestClient, make_user) -> None:
        owner = make_user(role="owner")
        prop = _property(client, owner["auth"])
        created = _turnover(client, owner["auth"], prop["id"])

        resp = client.post(
            f"/api/turnovers/{created['id']}/cancel",
            json={"reason": "Guest extended their stay."},
            headers=owner["auth"],
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "cancelled"
        assert body["cancelled_at"] is not None
        assert body["cancellation_reason"] == "Guest extended their stay."

    def test_an_awarded_turnover_cannot_be_quietly_rescheduled(
        self, client: TestClient, make_user, db
    ) -> None:
        """A cleaner has arranged their day around this. Moving it is not a PATCH.

        Phase 4 owns that path — notifying the cleaner, re-posting, the late-
        cancellation policy. Allowing it here would let the cleaner find out by
        showing up to an empty house.
        """
        import uuid as _uuid

        from app.models import Turnover, TurnoverStatus

        owner = make_user(role="owner")
        prop = _property(client, owner["auth"])
        created = _turnover(client, owner["auth"], prop["id"])

        row = db.get(Turnover, _uuid.UUID(created["id"]))
        row.status = TurnoverStatus.AWARDED
        db.commit()

        new_checkout = datetime.now(timezone.utc) + timedelta(days=2)
        resp = client.patch(
            f"/api/turnovers/{created['id']}",
            json={"checkout_at": new_checkout.isoformat()},
            headers=owner["auth"],
        )
        assert resp.status_code == 409

    def test_an_awarded_turnover_cannot_be_quietly_cancelled(
        self, client: TestClient, make_user, db
    ) -> None:
        """Phase 4 made this a real path; it did not make it a quiet one.

        Until phase 4 this endpoint refused an awarded turnover outright and
        said so. Now it cancels the booking, tells the cleaner and an admin, and
        demands a reason first — so the "quietly" in this test's name is what it
        still guards. The full path, including who gets told, is covered in
        test_cancellation.py; here it is enough that a bare cancel bounces.
        """
        import uuid as _uuid

        from app.models import Award, Turnover, TurnoverStatus

        owner = make_user(role="owner")
        prop = _property(client, owner["auth"])
        created = _turnover(client, owner["auth"], prop["id"])
        cleaner = make_user(role="cleaner")

        row = db.get(Turnover, _uuid.UUID(created["id"]))
        row.status = TurnoverStatus.AWARDED
        db.add(
            Award(
                turnover_id=row.id,
                cleaner_id=_uuid.UUID(cleaner["user"]["id"]),
                agreed_price_cents=12_000,
            )
        )
        db.commit()

        resp = client.post(
            f"/api/turnovers/{created['id']}/cancel", json={}, headers=owner["auth"]
        )
        assert resp.status_code == 422
        assert "Give a reason" in resp.json()["detail"]

        db.refresh(row)
        assert row.status is TurnoverStatus.AWARDED

    def test_a_cancelled_turnover_cannot_be_republished(
        self, client: TestClient, make_user
    ) -> None:
        owner = make_user(role="owner")
        prop = _property(client, owner["auth"])
        created = _turnover(client, owner["auth"], prop["id"])
        client.post(f"/api/turnovers/{created['id']}/cancel", json={}, headers=owner["auth"])

        resp = client.post(f"/api/turnovers/{created['id']}/publish", headers=owner["auth"])
        assert resp.status_code == 409


class TestEveryTurnoverResponseHasTheSameShape:
    """Regression: an action that answers with less than the GET blanks the screen.

    The detail screen replaces its state with whatever an action returns. When
    `cancel` answered without the nested property, the next render read
    `property.nickname` off `undefined` and the page went blank — right after a
    click, with a 200 on the wire and nothing in the server log. Caught by
    clicking through it in a browser, not by any endpoint test that existed at
    the time.

    Every endpoint returning a single turnover returns the detail shape.
    """

    def test_create_publish_patch_and_cancel_all_carry_the_property(
        self, client: TestClient, make_user
    ) -> None:
        owner = make_user(role="owner")
        prop = _property(client, owner["auth"])

        created = client.post(
            "/api/turnovers",
            json={
                "property_id": prop["id"],
                "checkout_at": (datetime.now(timezone.utc) + timedelta(days=6)).isoformat(),
                "publish": False,
            },
            headers=owner["auth"],
        )
        assert created.status_code == 201
        turnover_id = created.json()["id"]

        responses = {
            "create": created,
            "get": client.get(f"/api/turnovers/{turnover_id}", headers=owner["auth"]),
            "patch": client.patch(
                f"/api/turnovers/{turnover_id}",
                json={"notes": "Extra towels."},
                headers=owner["auth"],
            ),
            "publish": client.post(
                f"/api/turnovers/{turnover_id}/publish", headers=owner["auth"]
            ),
            "cancel": client.post(
                f"/api/turnovers/{turnover_id}/cancel", json={}, headers=owner["auth"]
            ),
        }

        for name, resp in responses.items():
            assert resp.status_code in (200, 201), f"{name}: {resp.text}"
            body = resp.json()
            assert "property" in body, f"{name} dropped the nested property"
            assert body["property"]["id"] == prop["id"], name
            assert body["property"]["nickname"] == prop["nickname"], name
