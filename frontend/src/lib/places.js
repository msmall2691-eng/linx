// Towns in the one region this product serves, with their centres.
//
// **Why a bundled list instead of a geocoding API.** One region is hardcoded
// (CLAUDE.md), so the set of places anybody can legitimately pick is small,
// known, and does not change. A geocoder would add an API key, a network
// dependency, a rate limit and a per-signup failure mode to a form whose entire
// job is turning "Biddeford" into two numbers — and it would still happily
// accept Phoenix.
//
// The coordinates are town-centre approximations, good to a few hundred metres.
// That is the right precision for the thing they feed: a service *radius* in
// whole miles. A cleaner saying "Saco, 25 miles" does not need their driveway.
//
// If the product ever serves a second region, this file is the thing that has
// to change — which is the point. A hardcoded region should be visibly
// hardcoded somewhere, rather than implied by a geocoder that would have
// cheerfully returned anywhere on earth.

export const REGION_LABEL = 'Southern Maine'

export const PLACES = [
  { name: 'Portland', lat: 43.6591, lng: -70.2568, zips: ['04101', '04102', '04103'] },
  { name: 'South Portland', lat: 43.6415, lng: -70.2409, zips: ['04106'] },
  { name: 'Cape Elizabeth', lat: 43.5651, lng: -70.2003, zips: ['04107'] },
  { name: 'Scarborough', lat: 43.5781, lng: -70.3217, zips: ['04074'] },
  { name: 'Westbrook', lat: 43.6770, lng: -70.3712, zips: ['04092'] },
  { name: 'Falmouth', lat: 43.7276, lng: -70.2420, zips: ['04105'] },
  { name: 'Cumberland', lat: 43.7959, lng: -70.2589, zips: ['04021'] },
  { name: 'Yarmouth', lat: 43.8004, lng: -70.1868, zips: ['04096'] },
  { name: 'Freeport', lat: 43.8570, lng: -70.1031, zips: ['04032'] },
  { name: 'Brunswick', lat: 43.9145, lng: -69.9653, zips: ['04011'] },
  { name: 'Topsham', lat: 43.9284, lng: -69.9756, zips: ['04086'] },
  { name: 'Bath', lat: 43.9109, lng: -69.8214, zips: ['04530'] },
  { name: 'Harpswell', lat: 43.8123, lng: -69.9781, zips: ['04079'] },
  { name: 'Gorham', lat: 43.6792, lng: -70.4425, zips: ['04038'] },
  { name: 'Windham', lat: 43.7862, lng: -70.4339, zips: ['04062'] },
  { name: 'Standish', lat: 43.7509, lng: -70.5539, zips: ['04084'] },
  { name: 'Old Orchard Beach', lat: 43.5173, lng: -70.3773, zips: ['04064'] },
  { name: 'Saco', lat: 43.5009, lng: -70.4428, zips: ['04072'] },
  { name: 'Biddeford', lat: 43.4926, lng: -70.4534, zips: ['04005'] },
  { name: 'Kennebunk', lat: 43.3845, lng: -70.5453, zips: ['04043'] },
  { name: 'Kennebunkport', lat: 43.3617, lng: -70.4767, zips: ['04046'] },
  { name: 'Wells', lat: 43.3223, lng: -70.5806, zips: ['04090'] },
  { name: 'Ogunquit', lat: 43.2484, lng: -70.5989, zips: ['03907'] },
  { name: 'York', lat: 43.1620, lng: -70.6462, zips: ['03909'] },
  { name: 'Kittery', lat: 43.0898, lng: -70.7364, zips: ['03904'] },
  { name: 'Eliot', lat: 43.1509, lng: -70.7989, zips: ['03903'] },
  { name: 'Berwick', lat: 43.2662, lng: -70.8664, zips: ['03901'] },
  { name: 'Sanford', lat: 43.4392, lng: -70.7742, zips: ['04073'] },
  { name: 'Lewiston', lat: 44.1004, lng: -70.2148, zips: ['04240'] },
  { name: 'Auburn', lat: 44.0979, lng: -70.2311, zips: ['04210'] },
]

/** Places whose name or ZIP matches what somebody has typed. */
export function searchPlaces(query, limit = 6) {
  const q = query.trim().toLowerCase()
  if (!q) return []

  return PLACES.filter(
    (place) =>
      place.name.toLowerCase().includes(q) ||
      place.zips.some((zip) => zip.startsWith(q)),
  ).slice(0, limit)
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
 * Only a label. The coordinates the browser gave are what gets saved — this is
 * so the screen can say "looks like Scarborough" instead of two decimals nobody
 * can check. Returns null when the fix is far outside the region, which is the
 * honest answer rather than naming the least-distant town in Maine.
 */
export function nearestPlace(lat, lng, withinMiles = 60) {
  let best = null
  let bestDistance = Infinity

  for (const place of PLACES) {
    const distance = milesBetween(lat, lng, place.lat, place.lng)
    if (distance < bestDistance) {
      best = place
      bestDistance = distance
    }
  }
  return bestDistance <= withinMiles ? best : null
}
