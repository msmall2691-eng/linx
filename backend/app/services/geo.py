"""Distance between a cleaner's service area and a property.

One region at launch, so this is deliberately plain great-circle distance — no
PostGIS, no routing, no drive-time estimates. Within a single metro the error
against road distance is not what decides whether a cleaner takes a job, and a
spatial extension is a dependency the pilot does not need.

The filtering happens in SQL rather than in Python: the bench board must be able
to page and order a radius query, which it cannot do if the radius is applied
after the rows come back.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import Float, cast, func
from sqlalchemy.sql.elements import ColumnElement

EARTH_RADIUS_MILES = 3958.7613


def haversine_miles(
    lat1: float | Decimal,
    lng1: float | Decimal,
    lat2: float | Decimal,
    lng2: float | Decimal,
) -> float:
    """Great-circle distance in miles. The Python twin of `distance_miles_sql`.

    Used for display and for tests; the SQL version is what filters queries.
    Both are here so the two can be checked against each other rather than
    drifting into two different answers to the same question.
    """
    import math

    lat1_r, lng1_r, lat2_r, lng2_r = (
        math.radians(float(v)) for v in (lat1, lng1, lat2, lng2)
    )
    dlat = lat2_r - lat1_r
    dlng = lng2_r - lng1_r
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlng / 2) ** 2
    )
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def distance_miles_sql(
    lat1: ColumnElement | float,
    lng1: ColumnElement | float,
    lat2: ColumnElement,
    lng2: ColumnElement,
) -> ColumnElement:
    """The same haversine, as a SQL expression for filtering and ordering."""
    lat1_r = func.radians(cast(lat1, Float))
    lng1_r = func.radians(cast(lng1, Float))
    lat2_r = func.radians(cast(lat2, Float))
    lng2_r = func.radians(cast(lng2, Float))

    dlat = lat2_r - lat1_r
    dlng = lng2_r - lng1_r

    a = func.power(func.sin(dlat / 2), 2) + func.cos(lat1_r) * func.cos(lat2_r) * func.power(
        func.sin(dlng / 2), 2
    )
    # asin's argument can exceed 1 by a float hair at antipodal points, which
    # would raise in Postgres rather than return a large distance.
    return 2 * EARTH_RADIUS_MILES * func.asin(func.least(func.sqrt(a), 1.0))
