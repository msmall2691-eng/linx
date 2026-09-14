"""The towns this product serves, and where they are.

**One region at launch** (CLAUDE.md), so the set of places anybody can
legitimately name is small, known, and does not change. That is what makes a
bundled table the right answer rather than a stopgap: a geocoder would add a
key, a network call, a rate limit and a failure mode to a question with thirty
possible answers, and would still cheerfully accept Phoenix.

This module is the **canonical copy**. The cleaner's town picker in the frontend
reads it through `GET /api/places` rather than carrying its own list, because
two tables of coordinates that must agree are two tables that eventually do not,
and the one that is wrong is the one nobody is looking at.

The coordinates are town centres, good to a few hundred metres. That is the
right precision for what they feed — a service *radius* in whole miles, and a
match between "somewhere in Saco" and "within 25 miles of Portland". Nothing
here is trying to find a doorstep.

If a second region ever happens, this file is what has to change. A hardcoded
region should be visibly hardcoded somewhere rather than implied by a service
that would have returned anywhere on earth.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Place:
    name: str
    lat: float
    lng: float
    zips: tuple[str, ...]


PLACES: tuple[Place, ...] = (
    Place("Portland", 43.6591, -70.2568, ("04101", "04102", "04103")),
    Place("South Portland", 43.6415, -70.2409, ("04106",)),
    Place("Cape Elizabeth", 43.5651, -70.2003, ("04107",)),
    Place("Scarborough", 43.5781, -70.3217, ("04074",)),
    Place("Westbrook", 43.6770, -70.3712, ("04092",)),
    Place("Falmouth", 43.7276, -70.2420, ("04105",)),
    Place("Cumberland", 43.7959, -70.2589, ("04021",)),
    Place("Yarmouth", 43.8004, -70.1868, ("04096",)),
    Place("Freeport", 43.8570, -70.1031, ("04032",)),
    Place("Brunswick", 43.9145, -69.9653, ("04011",)),
    Place("Topsham", 43.9284, -69.9756, ("04086",)),
    Place("Bath", 43.9109, -69.8214, ("04530",)),
    Place("Harpswell", 43.8123, -69.9781, ("04079",)),
    Place("Gorham", 43.6792, -70.4425, ("04038",)),
    Place("Windham", 43.7862, -70.4339, ("04062",)),
    Place("Standish", 43.7509, -70.5539, ("04084",)),
    Place("Old Orchard Beach", 43.5173, -70.3773, ("04064",)),
    Place("Saco", 43.5009, -70.4428, ("04072",)),
    Place("Biddeford", 43.4926, -70.4534, ("04005",)),
    Place("Kennebunk", 43.3845, -70.5453, ("04043",)),
    Place("Kennebunkport", 43.3617, -70.4767, ("04046",)),
    Place("Wells", 43.3223, -70.5806, ("04090",)),
    Place("Ogunquit", 43.2484, -70.5989, ("03907",)),
    Place("York", 43.1620, -70.6462, ("03909",)),
    Place("Kittery", 43.0898, -70.7364, ("03904",)),
    Place("Eliot", 43.1509, -70.7989, ("03903",)),
    Place("Berwick", 43.2662, -70.8664, ("03901",)),
    Place("Sanford", 43.4392, -70.7742, ("04073",)),
    Place("Lewiston", 44.1004, -70.2148, ("04240",)),
    Place("Auburn", 44.0979, -70.2311, ("04210",)),
)

_BY_ZIP = {zip_code: place for place in PLACES for zip_code in place.zips}
_BY_NAME = {place.name.casefold(): place for place in PLACES}


def by_zip(postal_code: str | None) -> Place | None:
    """The town a ZIP belongs to. Takes ZIP+4 and ignores the +4."""
    if not postal_code:
        return None
    match = re.match(r"\s*(\d{5})", postal_code)
    return _BY_ZIP.get(match.group(1)) if match else None


def by_name(city: str | None) -> Place | None:
    """The town by name, case- and whitespace-insensitively."""
    if not city:
        return None
    return _BY_NAME.get(city.strip().casefold())


def miles_between(
    lat1: float, lng1: float, lat2: float, lng2: float
) -> float:
    """Great-circle miles. Mirrors `geo.haversine_miles` for this module's own
    use so a place lookup does not import the query layer."""
    earth_radius_miles = 3958.7613
    lat1_r, lng1_r, lat2_r, lng2_r = (
        math.radians(v) for v in (lat1, lng1, lat2, lng2)
    )
    dlat, dlng = lat2_r - lat1_r, lng2_r - lng1_r
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlng / 2) ** 2
    return 2 * earth_radius_miles * math.asin(math.sqrt(h))


def nearest(lat: float, lng: float, *, within_miles: float = 60.0) -> Place | None:
    """The closest town to a fix, or None if it is far outside the region.

    The `within_miles` guard is what stops this from confidently naming the
    least-distant town in Maine for a set of coordinates in Arizona. Outside the
    region the honest answer is "nowhere near here".
    """
    best: Place | None = None
    best_distance = math.inf
    for place in PLACES:
        distance = miles_between(lat, lng, place.lat, place.lng)
        if distance < best_distance:
            best, best_distance = place, distance
    return best if best_distance <= within_miles else None
