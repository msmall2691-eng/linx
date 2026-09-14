// The towns this product serves, fetched from the server.
//
// **There is one table and it lives in the backend** (`app/services/places.py`),
// because the same coordinates decide two different things: where a cleaner's
// service circle is centred, and where a property sits when its address cannot
// be geocoded exactly. Two copies that have to agree are two copies that
// eventually do not, and the one that is wrong is the one nobody is looking at.
//
// So this module holds the *helpers* — searching, distance, nearest — and
// fetches the data. The helpers are pure and take the list as an argument, so
// they are testable without a network and cannot accidentally close over a
// stale copy.

import { apiFetch } from './api.js'

let cached = null
let inFlight = null

/**
 * The town list, fetched once per page load.
 *
 * Public and unauthenticated on the server, because a cleaner picks their
 * service area before they have an account.
 */
export function loadPlaces() {
  if (cached) return Promise.resolve(cached)
  if (inFlight) return inFlight

  inFlight = apiFetch('/places', { auth: false })
    .then((places) => {
      cached = places
      inFlight = null
      return places
    })
    .catch((err) => {
      // Let the next attempt try again rather than caching a failure — a blip
      // during one render should not disable the picker for the whole session.
      inFlight = null
      throw err
    })
  return inFlight
}

/** Places whose name or ZIP matches what somebody has typed. */
export function searchPlaces(places, query, limit = 6) {
  const q = query.trim().toLowerCase()
  if (!q) return []

  return places
    .filter(
      (place) =>
        place.name.toLowerCase().includes(q) ||
        place.zips.some((zip) => zip.startsWith(q)),
    )
    .slice(0, limit)
}

/** Great-circle distance in miles, for naming the town nearest a fix. */
export function milesBetween(aLat, aLng, bLat, bLng) {
  const toRad = (deg) => (deg * Math.PI) / 180
  const earthRadiusMiles = 3958.8

  const dLat = toRad(bLat - aLat)
  const dLng = toRad(bLng - aLng)
  const h =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(aLat)) * Math.cos(toRad(bLat)) * Math.sin(dLng / 2) ** 2
  return 2 * earthRadiusMiles * Math.asin(Math.sqrt(h))
}

/**
 * The nearest town to a set of coordinates, for showing somebody where the
 * browser thinks they are.
 *
 * Only a label — the coordinates the browser gave are what gets saved. Returns
 * null when the fix is far outside the region, which is the honest answer
 * rather than naming the least-distant town in Maine for somewhere in Arizona.
 */
export function nearestPlace(places, lat, lng, withinMiles = 60) {
  let best = null
  let bestDistance = Infinity

  for (const place of places) {
    const distance = milesBetween(lat, lng, place.lat, place.lng)
    if (distance < bestDistance) {
      best = place
      bestDistance = distance
    }
  }
  return bestDistance <= withinMiles ? best : null
}
