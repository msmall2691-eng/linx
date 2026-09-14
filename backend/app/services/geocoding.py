"""Turning a property's address into coordinates.

**This closes a hole that silently switched the marketplace off.** The bench
board matches a turnover to a cleaner on distance, and its query requires
`Property.lat IS NOT NULL`. Nothing in the application ever set those columns —
only the browser tests did, by writing straight to the database. So every
property created through the real site had null coordinates and appeared on
nobody's board. Every screen worked; the product did not.

The fix is a rule, not a field: **a property always ends up with coordinates**,
and there are two ways to get them, tried in order.

1. **What the client sent.** When Google Places autocomplete is configured, the
   address the owner picked comes back with exact coordinates attached, and
   those are the best answer available — they describe the building rather than
   the town.

2. **The region's own table** (`app/services/places.py`). Failing anything
   better, a ZIP or a town name resolves to that town's centre. Within a
   single metro, matching "somewhere in Saco" against "within 25 miles of
   Portland" gives the same answer a rooftop fix would, because the radius is
   in whole miles.

**No key configured means less precise, never broken** — the same posture as
Checkr's manual fallback and SMTP's logging sender, and deliberately unlike
Stripe, where nothing may stand in for money. A missing geocoder costs a few
hundred metres of accuracy on a radius measured in miles. A property with no
coordinates costs the entire marketplace.

The last resort is the region centre, used only when an address names no ZIP and
no town this product serves. It is deliberately *not* null: a null here is the
bug this module exists to prevent, and a property matched slightly too
generously is visible to a few cleaners who are a little far away — an
irritation, where invisibility is silence.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.services import places


@dataclass(frozen=True)
class Fix:
    """Coordinates, and how confident we are about where they came from."""

    lat: float
    lng: float
    #: "client" (exact, from an address the owner picked), "zip", "city", or
    #: "region" (the fallback of last resort). Stored nowhere yet; it exists so
    #: a caller can log or explain a coarse match rather than pretend to
    #: precision it does not have.
    source: str


#: Where the region sits, for an address that names nowhere we recognise. The
#: centre of the largest town we serve rather than an average of all of them:
#: an average is a point in the sea.
REGION_CENTRE = places.by_name("Portland")


def _valid(lat: Decimal | float | None, lng: Decimal | float | None) -> bool:
    """Whether a client-supplied pair is usable at all.

    (0, 0) is in the Atlantic off Africa and is what an uninitialised form
    field looks like, so it is rejected rather than saved as a location.
    """
    if lat is None or lng is None:
        return False
    lat_f, lng_f = float(lat), float(lng)
    if not (-90 <= lat_f <= 90 and -180 <= lng_f <= 180):
        return False
    return not (lat_f == 0 and lng_f == 0)


def locate(
    *,
    lat: Decimal | float | None = None,
    lng: Decimal | float | None = None,
    postal_code: str | None = None,
    city: str | None = None,
) -> Fix:
    """Coordinates for a property. **Always returns a fix.**

    Callers pass whatever they have. The point of the signature is that there
    is no way to ask this question and get "nothing" back — the null case is
    the failure the whole module exists to remove.
    """
    if _valid(lat, lng):
        return Fix(lat=float(lat), lng=float(lng), source="client")

    place = places.by_zip(postal_code)
    if place is not None:
        return Fix(lat=place.lat, lng=place.lng, source="zip")

    place = places.by_name(city)
    if place is not None:
        return Fix(lat=place.lat, lng=place.lng, source="city")

    centre = REGION_CENTRE
    assert centre is not None, "the region table must contain its own centre"
    return Fix(lat=centre.lat, lng=centre.lng, source="region")
