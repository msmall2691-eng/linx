import { useEffect, useRef, useState } from 'react'

import { useConfig } from '../lib/config.jsx'

/**
 * The address of a property, with autocomplete when a key is configured.
 *
 * **The coordinates are the point.** A property's lat/lng is what the bench
 * board matches on, and a property without them is invisible to every cleaner
 * on a screen that looks completely normal — which is exactly what was
 * happening before this existed. Autocomplete is the precise way to get them:
 * the place somebody picks comes back with the building's own coordinates
 * attached.
 *
 * **Without a key it degrades, it does not break.** The fields become plain
 * text and the server places the property from its ZIP or town
 * (`app/services/geocoding.py`). That costs a few hundred metres of accuracy on
 * a radius measured in whole miles. It is the same posture as Checkr's manual
 * fallback and SMTP's logging sender — and deliberately unlike Stripe, where
 * nothing stands in for money.
 *
 * The key this reads is a **browser** key. The Maps SDK runs in the page, so
 * the key is in the page by definition; Google's model is to restrict it by
 * HTTP referrer rather than hide it. That restriction is not optional — an
 * unrestricted Maps key in a public page is somebody else's autocomplete on
 * your bill.
 */
const SCRIPT_ID = 'google-maps-places'

function loadPlaces(apiKey) {
  if (window.google?.maps?.places) return Promise.resolve(window.google.maps.places)

  const existing = document.getElementById(SCRIPT_ID)
  if (existing) {
    return new Promise((resolve, reject) => {
      existing.addEventListener('load', () => resolve(window.google.maps.places))
      existing.addEventListener('error', reject)
    })
  }

  return new Promise((resolve, reject) => {
    const script = document.createElement('script')
    script.id = SCRIPT_ID
    script.async = true
    script.src = `https://maps.googleapis.com/maps/api/js?key=${encodeURIComponent(
      apiKey,
    )}&libraries=places&loading=async`
    script.addEventListener('load', () => resolve(window.google?.maps?.places))
    script.addEventListener('error', () => reject(new Error('Maps failed to load')))
    document.head.appendChild(script)
  })
}

/** Pull the pieces we store out of a Places result. */
function partsOf(place) {
  const get = (type, form = 'short_name') =>
    place.address_components?.find((c) => c.types.includes(type))?.[form] ?? ''

  const streetNumber = get('street_number')
  const route = get('route', 'long_name')

  return {
    address_line1: [streetNumber, route].filter(Boolean).join(' '),
    city:
      get('locality', 'long_name') ||
      get('sublocality', 'long_name') ||
      get('administrative_area_level_3', 'long_name'),
    state: get('administrative_area_level_1'),
    postal_code: get('postal_code'),
    lat: place.geometry?.location?.lat(),
    lng: place.geometry?.location?.lng(),
  }
}

export default function AddressFields({ form, onChange, onPick }) {
  const { google_maps_api_key: apiKey } = useConfig()
  const [ready, setReady] = useState(false)
  const [failed, setFailed] = useState(false)
  const line1 = useRef(null)

  useEffect(() => {
    if (!apiKey || !line1.current) return

    let widget = null
    let cancelled = false

    loadPlaces(apiKey)
      .then((placesLib) => {
        if (cancelled || !placesLib || !line1.current) return
        widget = new placesLib.Autocomplete(line1.current, {
          types: ['address'],
          // One region at launch, so there is no reason to offer an address in
          // another country and every reason not to.
          componentRestrictions: { country: 'us' },
          fields: ['address_components', 'geometry'],
        })
        widget.addListener('place_changed', () => {
          const parts = partsOf(widget.getPlace())
          if (!parts.address_line1) return
          onPick(parts)
        })
        setReady(true)
      })
      .catch(() => {
        // A blocked script or a bad key must not take the form with it. The
        // plain fields underneath are still a complete, submittable address.
        if (!cancelled) setFailed(true)
      })

    return () => {
      cancelled = true
      if (widget) window.google?.maps?.event?.clearInstanceListeners(widget)
    }
  }, [apiKey, onPick])

  return (
    <>
      <div>
        <label htmlFor="address_line1" className="field-label">
          Street address
        </label>
        <input
          id="address_line1"
          ref={line1}
          required
          value={form.address_line1}
          onChange={onChange('address_line1')}
          placeholder={ready ? 'Start typing the address…' : '12 Ocean Ave'}
          className="field-input"
          autoComplete="off"
        />
        {ready && (
          <p className="mt-1 text-xs text-slate-500">
            Pick it from the list and the rest fills in.
          </p>
        )}
        {failed && (
          <p className="mt-1 text-xs text-slate-500">
            Address lookup is unavailable right now — type it in and we&rsquo;ll place
            it from the ZIP.
          </p>
        )}
      </div>

      <div>
        <label htmlFor="address_line2" className="field-label">
          Unit <span className="font-normal text-slate-400">(optional)</span>
        </label>
        <input
          id="address_line2"
          value={form.address_line2}
          onChange={onChange('address_line2')}
          placeholder="Apt 2"
          className="field-input"
        />
      </div>

      <div className="grid gap-4 sm:grid-cols-3">
        <div className="sm:col-span-1">
          <label htmlFor="city" className="field-label">
            Town
          </label>
          <input
            id="city"
            required
            value={form.city}
            onChange={onChange('city')}
            className="field-input"
          />
        </div>
        <div>
          <label htmlFor="state" className="field-label">
            State
          </label>
          <input
            id="state"
            required
            maxLength={2}
            value={form.state}
            onChange={onChange('state')}
            placeholder="ME"
            className="field-input"
          />
        </div>
        <div>
          <label htmlFor="postal_code" className="field-label">
            ZIP
          </label>
          <input
            id="postal_code"
            required
            value={form.postal_code}
            onChange={onChange('postal_code')}
            placeholder="04101"
            className="field-input"
          />
          {/* Said plainly because it is load-bearing: without autocomplete the
              ZIP is what puts this property on the map, and a property that is
              not on the map is one no cleaner ever sees. */}
          <p className="mt-1 text-xs text-slate-500">Used to find cleaners nearby.</p>
        </div>
      </div>
    </>
  )
}
