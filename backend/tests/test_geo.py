"""Service-radius geometry.

There are two implementations of the same haversine — one in Python for display
and one as a SQL expression for filtering — so the first thing these tests do is
hold them to the same answer. Two functions claiming to compute one distance is
exactly the shape that drifts.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Float, cast, literal, select
from sqlalchemy.orm import Session

from app.services.geo import distance_miles_sql, haversine_miles

# Real coordinates, so the expected distances are checkable against a map.
PORTLAND_ME = (43.6591, -70.2568)
OLD_ORCHARD_BEACH = (43.5148, -70.3778)  # ~12 mi south of Portland
BRUNSWICK_ME = (43.9145, -69.9653)  # ~23 mi north-east
BOSTON_MA = (42.3601, -71.0589)  # ~98 mi south-west


def _sql_distance(db: Session, a: tuple[float, float], b: tuple[float, float]) -> float:
    expression = distance_miles_sql(
        a[0], a[1], cast(literal(b[0]), Float), cast(literal(b[1]), Float)
    )
    return float(db.execute(select(expression)).scalar_one())


class TestHaversine:
    def test_zero_distance_to_itself(self) -> None:
        assert haversine_miles(*PORTLAND_ME, *PORTLAND_ME) == pytest.approx(0, abs=1e-9)

    @pytest.mark.parametrize(
        # Straight-line, so each is shorter than the drive: Portland to Boston
        # is ~98 miles as the crow flies against ~110 by road.
        ("destination", "expected_miles"),
        [
            (OLD_ORCHARD_BEACH, 11.67),
            (BRUNSWICK_ME, 22.86),
            (BOSTON_MA, 98.48),
        ],
    )
    def test_known_distances(
        self, destination: tuple[float, float], expected_miles: float
    ) -> None:
        actual = haversine_miles(*PORTLAND_ME, *destination)
        assert actual == pytest.approx(expected_miles, rel=0.02)

    def test_it_is_symmetric(self) -> None:
        there = haversine_miles(*PORTLAND_ME, *BRUNSWICK_ME)
        back = haversine_miles(*BRUNSWICK_ME, *PORTLAND_ME)
        assert there == pytest.approx(back)


class TestTheSqlVersionAgrees:
    """The board filters in SQL; the profile displays in Python. Same number."""

    @pytest.mark.parametrize(
        "destination", [OLD_ORCHARD_BEACH, BRUNSWICK_ME, BOSTON_MA, PORTLAND_ME]
    )
    def test_sql_matches_python(self, db: Session, destination: tuple[float, float]) -> None:
        in_python = haversine_miles(*PORTLAND_ME, *destination)
        in_sql = _sql_distance(db, PORTLAND_ME, destination)
        assert in_sql == pytest.approx(in_python, rel=1e-6, abs=1e-6)

    def test_antipodal_points_do_not_blow_up(self, db: Session) -> None:
        """asin's argument can exceed 1 by a float hair, which Postgres rejects.

        Clamped in the SQL for exactly this case — without it the board query
        raises instead of returning a very large distance.
        """
        antipode = (-PORTLAND_ME[0], PORTLAND_ME[1] + 180)
        distance = _sql_distance(db, PORTLAND_ME, antipode)
        assert distance == pytest.approx(12436, rel=0.01)
