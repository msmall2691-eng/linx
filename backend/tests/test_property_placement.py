"""A property always has coordinates — the bug that switched the marketplace off.

**What happened.** The bench board matches a turnover to a cleaner on distance,
and its query carries `Property.lat IS NOT NULL`. Nothing in the application
ever set those columns. The browser tests set them by writing straight to the
database, which is exactly why they passed: every one of them placed its own
property before looking at a board.

So on the live site, every property had null coordinates and every turnover was
invisible to every cleaner. No endpoint errored. No log line appeared. The owner
posted a job and watched nobody bid.

The tests here are written against that failure rather than around it. They go
through the API the way a person does — no fixture reaches into the database to
place anything — because a test that sets `lat` itself is a test that cannot
notice the application never did.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.property import Property
from app.services import geocoding, places
from app.tasks import scheduled


def _create(client: TestClient, owner: dict, **overrides) -> dict:
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


class TestEveryPropertyGetsPlaced:
    def test_a_property_created_through_the_api_has_coordinates(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """**The regression test for the silent hole.**

        Nothing here writes `lat` — that is the whole point. Before this rule,
        the assertion below failed against a completely healthy-looking 201.
        """
        owner = make_user(role="owner")
        created = _create(client, owner)

        prop = db.get(Property, uuid.UUID(created["id"]))
        assert prop.lat is not None and prop.lng is not None, (
            "a property was saved with no coordinates, which makes it invisible "
            "to every cleaner on a screen that looks entirely normal"
        )

    def test_the_zip_decides_where_it_is(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """A Saco ZIP puts it in Saco, not at the region's default."""
        owner = make_user(role="owner")
        created = _create(client, owner, city="Saco", postal_code="04072")

        saco = places.by_name("Saco")
        prop = db.get(Property, uuid.UUID(created["id"]))
        assert round(float(prop.lat), 3) == round(saco.lat, 3)
        assert round(float(prop.lng), 3) == round(saco.lng, 3)

    def test_zip_plus_four_still_resolves(self, client: TestClient, make_user, db: Session) -> None:
        """People write ZIP+4. It must not fall through to the region centre."""
        owner = make_user(role="owner")
        created = _create(client, owner, city="Biddeford", postal_code="04005-1234")

        biddeford = places.by_name("Biddeford")
        prop = db.get(Property, uuid.UUID(created["id"]))
        assert round(float(prop.lat), 3) == round(biddeford.lat, 3)

    def test_exact_coordinates_from_the_client_win(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """Autocomplete describes the building; the ZIP only describes the town."""
        owner = make_user(role="owner")
        created = _create(client, owner, lat="43.5123", lng="-70.3456")

        prop = db.get(Property, uuid.UUID(created["id"]))
        assert round(float(prop.lat), 4) == 43.5123
        assert round(float(prop.lng), 4) == -70.3456

    def test_an_unknown_address_still_lands_somewhere(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """**Null is the one answer that is not allowed.**

        A property placed at the region centre is visible to cleaners who are a
        little far away, which is an irritation. A property placed nowhere is
        visible to nobody, which is silence — and silence is what this replaced.
        """
        owner = make_user(role="owner")
        created = _create(client, owner, city="Nowhere", postal_code="99999")

        prop = db.get(Property, uuid.UUID(created["id"]))
        assert prop.lat is not None and prop.lng is not None


class TestMovingAProperty:
    def test_changing_the_address_moves_the_coordinates(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """Stale coordinates are worse than coarse ones — they are confidently
        wrong, and would advertise a job at the previous house."""
        owner = make_user(role="owner")
        created = _create(client, owner, city="Portland", postal_code="04101")

        resp = client.patch(
            f"/api/properties/{created['id']}",
            json={"city": "Kittery", "postal_code": "03904"},
            headers=owner["auth"],
        )
        assert resp.status_code == 200, resp.text

        kittery = places.by_name("Kittery")
        db.expire_all()
        prop = db.get(Property, uuid.UUID(created["id"]))
        assert round(float(prop.lat), 3) == round(kittery.lat, 3)

    def test_an_edit_that_does_not_touch_the_address_leaves_them_alone(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """Renaming a property must not re-place it — a precise fix from
        autocomplete would be thrown away for a town centre."""
        owner = make_user(role="owner")
        created = _create(client, owner, lat="43.5123", lng="-70.3456")

        resp = client.patch(
            f"/api/properties/{created['id']}",
            json={"nickname": "The Cottage"},
            headers=owner["auth"],
        )
        assert resp.status_code == 200, resp.text

        db.expire_all()
        prop = db.get(Property, uuid.UUID(created["id"]))
        assert round(float(prop.lat), 4) == 43.5123


class TestTheBackfill:
    def test_the_scheduled_pass_places_a_property_that_has_none(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        """For the properties saved before the rule existed.

        This is the only test here allowed to write a null into the database,
        because it is reproducing the state the live site is already in.
        """
        owner = make_user(role="owner")
        created = _create(client, owner, city="Wells", postal_code="04090")

        prop = db.get(Property, uuid.UUID(created["id"]))
        prop.lat = prop.lng = None
        db.commit()

        assert scheduled.place_unmapped_properties(db) == 1

        db.expire_all()
        prop = db.get(Property, uuid.UUID(created["id"]))
        wells = places.by_name("Wells")
        assert round(float(prop.lat), 3) == round(wells.lat, 3)

    def test_it_does_nothing_when_everything_is_placed(
        self, client: TestClient, make_user, db: Session
    ) -> None:
        owner = make_user(role="owner")
        _create(client, owner)
        assert scheduled.place_unmapped_properties(db) == 0

    def test_a_placed_property_actually_reaches_the_board(
        self, client: TestClient, make_user, make_cleaner, db: Session
    ) -> None:
        """**The end of the story, asserted rather than assumed.**

        Coordinates are not the point; being biddable is. This walks it the way
        the product does — create a property through the API, post a turnover,
        and look at a real cleaner's board — with nothing reaching into the
        database to place anything.
        """
        owner = make_user(role="owner")
        prop = _create(client, owner, city="Portland", postal_code="04101")

        posted = client.post(
            "/api/turnovers",
            json={
                "property_id": prop["id"],
                "checkout_at": "2027-07-04T15:00:00+00:00",
                "checkin_at": "2027-07-04T20:00:00+00:00",
                "owner_budget_cents": 15_000,
            },
            headers=owner["auth"],
        )
        assert posted.status_code == 201, posted.text

        cleaner = make_cleaner(cleared=True)
        client.put(
            "/api/cleaner/profile",
            json={
                "service_lat": str(places.by_name("Portland").lat),
                "service_lng": str(places.by_name("Portland").lng),
                "service_radius_miles": 25,
            },
            headers=cleaner["auth"],
        )

        board = client.get("/api/board", headers=cleaner["auth"])
        assert board.status_code == 200, board.text
        ids = [row["id"] for row in board.json()]
        assert posted.json()["id"] in ids, (
            "the turnover never reached a cleaner's board — which is what a "
            "property with no coordinates looks like from the outside"
        )


class TestTheLocateRuleItself:
    def test_it_never_returns_nothing(self) -> None:
        """The signature is the guarantee: there is no null answer to ask for."""
        fix = geocoding.locate()
        assert fix.lat is not None and fix.lng is not None
        assert fix.source == "region"

    @pytest.mark.parametrize("lat,lng", [(0, 0), (None, 10), (10, None), (91, 0)])
    def test_junk_coordinates_are_refused_rather_than_stored(self, lat, lng) -> None:
        """(0, 0) is the Atlantic off Africa, and it is what an uninitialised
        form field looks like. A property is not there."""
        fix = geocoding.locate(lat=lat, lng=lng, postal_code="04101")
        assert fix.source == "zip"

    def test_the_source_says_how_good_the_answer_is(self) -> None:
        assert geocoding.locate(lat=43.5, lng=-70.3).source == "client"
        assert geocoding.locate(postal_code="04101").source == "zip"
        assert geocoding.locate(city="Saco").source == "city"
        assert geocoding.locate(city="Atlantis").source == "region"


class TestTheTownTable:
    def test_the_frontend_reads_the_same_list_the_backend_uses(
        self, client: TestClient
    ) -> None:
        """One table, served, rather than two that must agree.

        Two copies of a coordinate table eventually disagree, and the wrong one
        is the one nobody is looking at.
        """
        resp = client.get("/api/places")
        assert resp.status_code == 200
        served = resp.json()
        assert len(served) == len(places.PLACES)
        assert {row["name"] for row in served} == {p.name for p in places.PLACES}
        assert all(row["zips"] for row in served)

    def test_every_zip_resolves_to_exactly_one_town(self) -> None:
        """A ZIP in two towns would place a property differently depending on
        which one the lookup happened to see first."""
        seen: dict[str, str] = {}
        for place in places.PLACES:
            for zip_code in place.zips:
                assert zip_code not in seen, (
                    f"{zip_code} is in both {seen.get(zip_code)} and {place.name}"
                )
                seen[zip_code] = place.name

    def test_nothing_in_the_table_is_outside_the_region(self) -> None:
        """A typo in a coordinate puts a town in the sea, and a property with
        it — which nothing else here would catch."""
        for place in places.PLACES:
            assert 42.8 < place.lat < 44.3, f"{place.name} is not in Maine"
            assert -71.2 < place.lng < -69.5, f"{place.name} is not in Maine"

    def test_the_region_centre_exists(self) -> None:
        """`geocoding` asserts this at the last resort; better to fail here."""
        assert geocoding.REGION_CENTRE is not None
