import { useEffect, useRef, useState } from 'react'

import { REGION_LABEL, nearestPlace, searchPlaces } from '../lib/places.js'

/**
 * Where a cleaner works, asked in a way a person can answer.
 *
 * **The form used to ask for latitude and longitude.** That is the coordinate
 * pair the matching runs on, so it is what the API takes — but nobody knows
 * their own latitude, and a signup form that opens with two decimal fields is a
 * signup form people close. This asks the question they can answer (which town)
 * or offers to work it out (use my location), and keeps the coordinates as
 * state nobody has to look at.
 *
 * Two ways in, because each fails where the other works:
 *
 * - **Use my location** is one tap and exact, and needs a permission prompt
 *   people sometimes refuse, on a device that sometimes has no fix.
 * - **Typing a town or ZIP** always works, needs no permission, and lets
 *   somebody set up an area they are not standing in — which is most people
 *   filling in a profile at their kitchen table for a town two over.
 *
 * Neither invents precision it does not have: a service *radius* in whole miles
 * is the thing these coordinates feed, so a town centre is close enough and
 * saying so is more honest than four decimal places.
 */
export default function ServiceAreaPicker({ lat, lng, onChange, radiusMiles }) {
  const [query, setQuery] = useState('')
  const [results, setResults] = useState([])
  const [locating, setLocating] = useState(false)
  const [locationError, setLocationError] = useState('')
  const [chosenLabel, setChosenLabel] = useState('')
  const wrapper = useRef(null)

  // When the profile loads with coordinates already saved, name them rather
  // than showing an empty box beside a set of numbers the person cannot read.
  useEffect(() => {
    if (chosenLabel || lat === '' || lng === '' || lat == null || lng == null) return
    const place = nearestPlace(Number(lat), Number(lng))
    setChosenLabel(place ? `${place.name}, ME` : 'Saved location')
  }, [lat, lng, chosenLabel])

  useEffect(() => {
    function onClickOutside(event) {
      if (wrapper.current && !wrapper.current.contains(event.target)) setResults([])
    }
    document.addEventListener('mousedown', onClickOutside)
    return () => document.removeEventListener('mousedown', onClickOutside)
  }, [])

  function search(value) {
    setQuery(value)
    setResults(searchPlaces(value))
  }

  function choose(place) {
    onChange({ lat: place.lat, lng: place.lng })
    setChosenLabel(`${place.name}, ME`)
    setQuery('')
    setResults([])
    setLocationError('')
  }

  function useMyLocation() {
    setLocationError('')
    if (!('geolocation' in navigator)) {
      setLocationError('This browser cannot share a location. Type a town instead.')
      return
    }

    setLocating(true)
    navigator.geolocation.getCurrentPosition(
      (position) => {
        const { latitude, longitude } = position.coords
        onChange({ lat: latitude, lng: longitude })
        const place = nearestPlace(latitude, longitude)
        // Name the nearest town so somebody can sanity-check the fix. If it is
        // nowhere near the region, say so plainly rather than silently
        // accepting a service area on the other side of the country.
        setChosenLabel(place ? `Near ${place.name}, ME` : 'Your current location')
        if (!place) {
          setLocationError(
            `That looks a long way from ${REGION_LABEL}. linx only covers this ` +
              'region right now — you can still save it, but no turnovers will ' +
              'be in range.',
          )
        }
        setLocating(false)
      },
      (err) => {
        setLocating(false)
        setLocationError(
          err.code === err.PERMISSION_DENIED
            ? 'No problem — type your town or ZIP instead.'
            : 'Could not get a location just now. Type your town or ZIP instead.',
        )
      },
      { enableHighAccuracy: false, timeout: 10000, maximumAge: 300000 },
    )
  }

  const hasArea = lat !== '' && lng !== '' && lat != null && lng != null

  return (
    <div ref={wrapper}>
      <span className="field-label">Where you work from</span>

      {hasArea && (
        <div
          className="mt-1 flex flex-wrap items-center justify-between gap-2 rounded-lg border border-brand-200 bg-brand-50 px-3 py-2"
          data-testid="service-area-chosen"
        >
          <span className="text-sm font-medium text-brand-900">{chosenLabel}</span>
          <span className="text-xs text-brand-800">
            {radiusMiles} mile{Number(radiusMiles) === 1 ? '' : 's'} around here
          </span>
        </div>
      )}

      <div className="mt-2 flex flex-wrap gap-2">
        <button
          type="button"
          onClick={useMyLocation}
          disabled={locating}
          className="btn-secondary"
          data-testid="use-my-location"
        >
          {locating ? 'Finding you…' : 'Use my current location'}
        </button>
        <span className="self-center text-sm text-slate-400">or</span>
      </div>

      <div className="relative mt-2">
        <label htmlFor="service-area-search" className="sr-only">
          Town or ZIP code
        </label>
        <input
          id="service-area-search"
          type="text"
          value={query}
          onChange={(e) => search(e.target.value)}
          placeholder={hasArea ? 'Change it — type a town or ZIP' : 'Type your town or ZIP'}
          className="field-input"
          autoComplete="off"
          data-testid="service-area-search"
        />

        {results.length > 0 && (
          <ul
            className="absolute z-10 mt-1 w-full overflow-hidden rounded-lg border border-slate-200 bg-white shadow-lg"
            data-testid="service-area-results"
          >
            {results.map((place) => (
              <li key={place.name}>
                <button
                  type="button"
                  onClick={() => choose(place)}
                  className="block w-full px-3 py-2 text-left text-sm hover:bg-slate-50"
                  data-testid={`place-${place.name.toLowerCase().replace(/\s+/g, '-')}`}
                >
                  <span className="font-medium">{place.name}</span>
                  <span className="ml-2 text-slate-500">{place.zips.join(' · ')}</span>
                </button>
              </li>
            ))}
          </ul>
        )}

        {query.trim().length > 1 && results.length === 0 && (
          <p className="mt-2 text-sm text-slate-600" data-testid="no-places">
            Nothing in {REGION_LABEL} matches that. linx covers one region right
            now — if your town is missing and it should be here, tell us.
          </p>
        )}
      </div>

      <p className="mt-2 text-xs text-slate-500">
        Turnovers show up on your board when they fall inside this circle. Roughly is
        fine — it feeds a radius in miles, not a doorstep.
      </p>

      {locationError && (
        <p className="mt-2 text-sm text-amber-700" data-testid="location-error">
          {locationError}
        </p>
      )}
    </div>
  )
}
