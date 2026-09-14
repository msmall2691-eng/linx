"""Owner-facing property endpoints, and the scoping that keeps owners apart."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

VALID_PROPERTY = {
    "nickname": "Seaside Cottage",
    "address_line1": "1 Harbor Way",
    "city": "Portland",
    "state": "ME",
    "postal_code": "04101",
    "bedrooms": 2,
    "bathrooms": "1.5",
    "access_notes": "Lockbox on the rail, code 4417.",
}


def _create(client: TestClient, auth: dict, **overrides) -> dict:
    resp = client.post("/api/properties", json={**VALID_PROPERTY, **overrides}, headers=auth)
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestCreate:
    def test_an_owner_can_add_a_property(self, client: TestClient, make_user) -> None:
        owner = make_user(role="owner")
        body = _create(client, owner["auth"])

        assert body["nickname"] == "Seaside Cottage"
        assert body["owner_id"] == owner["user"]["id"]
        assert body["is_active"] is True
        assert body["bathrooms"] == "1.5"

    def test_state_is_normalized_to_uppercase(self, client: TestClient, make_user) -> None:
        owner = make_user(role="owner")
        assert _create(client, owner["auth"], state="me")["state"] == "ME"

    def test_quarter_baths_are_rejected(self, client: TestClient, make_user) -> None:
        owner = make_user(role="owner")
        resp = client.post(
            "/api/properties",
            json={**VALID_PROPERTY, "bathrooms": "1.25"},
            headers=owner["auth"],
        )
        assert resp.status_code == 422

    def test_a_cleaner_cannot_add_a_property(self, client: TestClient, make_user) -> None:
        cleaner = make_user(role="cleaner")
        resp = client.post("/api/properties", json=VALID_PROPERTY, headers=cleaner["auth"])
        assert resp.status_code == 403

    def test_an_anonymous_caller_cannot_add_a_property(self, client: TestClient) -> None:
        assert client.post("/api/properties", json=VALID_PROPERTY).status_code == 401


class TestScoping:
    """An owner's properties are theirs alone."""

    def test_the_list_shows_only_your_own(self, client: TestClient, make_user) -> None:
        first = make_user(role="owner")
        second = make_user(role="owner")
        _create(client, first["auth"], nickname="Mine")
        _create(client, second["auth"], nickname="Theirs")

        body = client.get("/api/properties", headers=first["auth"]).json()
        assert [p["nickname"] for p in body] == ["Mine"]

    def test_another_owners_property_reads_as_missing_not_forbidden(
        self, client: TestClient, make_user
    ) -> None:
        """404, not 403 — a 403 would confirm the id exists."""
        first = make_user(role="owner")
        second = make_user(role="owner")
        theirs = _create(client, second["auth"])

        resp = client.get(f"/api/properties/{theirs['id']}", headers=first["auth"])
        assert resp.status_code == 404

    def test_another_owner_cannot_edit_it(self, client: TestClient, make_user) -> None:
        first = make_user(role="owner")
        second = make_user(role="owner")
        theirs = _create(client, second["auth"])

        resp = client.patch(
            f"/api/properties/{theirs['id']}",
            json={"nickname": "Hijacked"},
            headers=first["auth"],
        )
        assert resp.status_code == 404

        # And the row is untouched.
        still = client.get(f"/api/properties/{theirs['id']}", headers=second["auth"]).json()
        assert still["nickname"] == "Seaside Cottage"

    def test_another_owner_cannot_archive_it(self, client: TestClient, make_user) -> None:
        first = make_user(role="owner")
        second = make_user(role="owner")
        theirs = _create(client, second["auth"])

        resp = client.delete(f"/api/properties/{theirs['id']}", headers=first["auth"])
        assert resp.status_code == 404


class TestUpdate:
    def test_a_patch_touches_only_the_fields_it_names(
        self, client: TestClient, make_user
    ) -> None:
        """An omitted field keeps its value rather than being reset to a default."""
        owner = make_user(role="owner")
        created = _create(client, owner["auth"])

        resp = client.patch(
            f"/api/properties/{created['id']}",
            json={"nickname": "Seaside Cottage (upstairs)"},
            headers=owner["auth"],
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["nickname"] == "Seaside Cottage (upstairs)"
        assert body["access_notes"] == VALID_PROPERTY["access_notes"]
        assert body["bedrooms"] == 2

    def test_access_notes_can_be_cleared_deliberately(
        self, client: TestClient, make_user
    ) -> None:
        owner = make_user(role="owner")
        created = _create(client, owner["auth"])

        resp = client.patch(
            f"/api/properties/{created['id']}",
            json={"access_notes": None},
            headers=owner["auth"],
        )
        assert resp.json()["access_notes"] is None


class TestArchive:
    def test_archiving_hides_it_from_the_list_without_deleting_it(
        self, client: TestClient, make_user
    ) -> None:
        owner = make_user(role="owner")
        created = _create(client, owner["auth"])

        assert client.delete(f"/api/properties/{created['id']}", headers=owner["auth"]).status_code == 204
        assert client.get("/api/properties", headers=owner["auth"]).json() == []

        # Still there, and still reachable by id.
        archived = client.get(f"/api/properties/{created['id']}", headers=owner["auth"])
        assert archived.status_code == 200
        assert archived.json()["is_active"] is False

        with_archived = client.get(
            "/api/properties", params={"include_archived": True}, headers=owner["auth"]
        ).json()
        assert len(with_archived) == 1

    def test_a_property_with_a_scheduled_turnover_cannot_be_archived(
        self, client: TestClient, make_user
    ) -> None:
        """Guardrail 3: a cleaner may already be scheduled to show up there.

        Letting the property vanish from the owner's list while the job stands
        is the silent kind of breakage — nothing errors, and the owner simply
        stops seeing something that is still happening.
        """
        owner = make_user(role="owner")
        prop = _create(client, owner["auth"])

        checkout = datetime.now(timezone.utc) + timedelta(days=3)
        client.post(
            "/api/turnovers",
            json={"property_id": prop["id"], "checkout_at": checkout.isoformat()},
            headers=owner["auth"],
        )

        resp = client.delete(f"/api/properties/{prop['id']}", headers=owner["auth"])
        assert resp.status_code == 409
        assert "turnover" in resp.json()["detail"].lower()

        assert client.get(f"/api/properties/{prop['id']}", headers=owner["auth"]).json()[
            "is_active"
        ] is True

    def test_archiving_works_once_the_turnover_is_cancelled(
        self, client: TestClient, make_user
    ) -> None:
        owner = make_user(role="owner")
        prop = _create(client, owner["auth"])

        checkout = datetime.now(timezone.utc) + timedelta(days=3)
        turnover = client.post(
            "/api/turnovers",
            json={"property_id": prop["id"], "checkout_at": checkout.isoformat()},
            headers=owner["auth"],
        ).json()

        client.post(f"/api/turnovers/{turnover['id']}/cancel", json={}, headers=owner["auth"])
        assert client.delete(f"/api/properties/{prop['id']}", headers=owner["auth"]).status_code == 204
