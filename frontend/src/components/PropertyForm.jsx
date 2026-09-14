import { useCallback, useState } from 'react'

import AddressFields from './AddressFields.jsx'
import Alert from './Alert.jsx'

const BLANK = {
  nickname: '',
  address_line1: '',
  address_line2: '',
  city: '',
  state: '',
  postal_code: '',
  property_type: 'short_term_rental',
  bedrooms: 1,
  bathrooms: '1',
  square_feet: '',
  default_checkout_time: '11:00',
  default_checkin_time: '16:00',
  access_notes: '',
  cleaning_notes: '',
}

export default function PropertyForm({ initial, onSubmit, submitLabel, error }) {
  const [form, setForm] = useState({ ...BLANK, ...(initial ?? {}) })
  const [submitting, setSubmitting] = useState(false)

  function update(field) {
    return (event) => setForm((prev) => ({ ...prev, [field]: event.target.value }))
  }

  // An address picked from autocomplete brings the building's own coordinates
  // with it, which is the best answer there is: the server's fallback can only
  // place a property at the centre of its town. Wrapped in useCallback because
  // the Places widget is set up in an effect that depends on this identity —
  // a new function every render would tear the widget down and rebuild it on
  // every keystroke.
  const pickAddress = useCallback((parts) => {
    setForm((prev) => ({ ...prev, ...parts }))
  }, [])

  async function handleSubmit(event) {
    event.preventDefault()
    setSubmitting(true)
    try {
      await onSubmit({
        ...form,
        bedrooms: Number(form.bedrooms),
        bathrooms: String(form.bathrooms),
        // Blank means "I don't know", which is a real answer here — a number
        // somebody guessed at is worse than no number, and cleaners price on
        // this when it is there.
        square_feet: form.square_feet === '' ? null : Number(form.square_feet),
        default_checkout_time: `${String(form.default_checkout_time).slice(0, 5)}:00`,
        default_checkin_time: `${String(form.default_checkin_time).slice(0, 5)}:00`,
        address_line2: form.address_line2 || null,
        // Only sent when autocomplete provided them. Absent, the server places
        // the property from its ZIP — never nothing, because a property with no
        // coordinates is invisible to every cleaner.
        lat: form.lat == null ? undefined : String(form.lat),
        lng: form.lng == null ? undefined : String(form.lng),
        access_notes: form.access_notes || null,
        cleaning_notes: form.cleaning_notes || null,
      })
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form onSubmit={handleSubmit} className="card mt-6 space-y-4">
      <Alert>{error}</Alert>

      {/* What kind of place this is decides how its jobs are scheduled: a
          rental's clean is the gap between guests, a home's is a date somebody
          picked. The posting form reads this and asks different questions. */}
      <fieldset>
        <legend className="field-label">What kind of place is it?</legend>
        <div className="mt-2 grid gap-2 sm:grid-cols-2">
          {[
            {
              value: 'short_term_rental',
              label: 'Short-term rental',
              hint: 'Airbnb, VRBO — cleaned between guests.',
            },
            {
              value: 'residential',
              label: 'A home',
              hint: 'Somebody lives there. Cleaned on a date you choose.',
            },
          ].map((option) => (
            <label
              key={option.value}
              className={`cursor-pointer rounded-lg border p-3 text-sm transition ${
                form.property_type === option.value
                  ? 'border-brand-500 bg-brand-50 ring-1 ring-brand-500'
                  : 'border-slate-300 hover:bg-slate-50'
              }`}
            >
              <input
                type="radio"
                name="property_type"
                value={option.value}
                checked={form.property_type === option.value}
                onChange={update('property_type')}
                className="sr-only"
              />
              <span className="block font-medium">{option.label}</span>
              <span className="mt-0.5 block text-xs text-slate-500">{option.hint}</span>
            </label>
          ))}
        </div>
      </fieldset>

      <div>
        <label htmlFor="nickname" className="field-label">
          What do you call it?
        </label>
        <input
          id="nickname"
          required
          maxLength={120}
          value={form.nickname}
          onChange={update('nickname')}
          placeholder="Seaside Cottage"
          className="field-input"
        />
      </div>

      <AddressFields form={form} onChange={update} onPick={pickAddress} />

      <div className="grid gap-4 sm:grid-cols-3">
        <div>
          <label htmlFor="bedrooms" className="field-label">
            Bedrooms
          </label>
          <input
            id="bedrooms"
            type="number"
            min={0}
            max={50}
            required
            value={form.bedrooms}
            onChange={update('bedrooms')}
            className="field-input"
          />
        </div>
        <div>
          <label htmlFor="bathrooms" className="field-label">
            Bathrooms
          </label>
          <input
            id="bathrooms"
            type="number"
            min={0}
            max={50}
            step="0.5"
            required
            value={form.bathrooms}
            onChange={update('bathrooms')}
            className="field-input"
          />
        </div>
        <div>
          <label htmlFor="square_feet" className="field-label">
            Square feet <span className="font-normal text-slate-400">(optional)</span>
          </label>
          <input
            id="square_feet"
            type="number"
            min={1}
            max={100000}
            value={form.square_feet ?? ''}
            onChange={update('square_feet')}
            placeholder="1800"
            className="field-input"
          />
          {/* Optional on purpose: plenty of owners genuinely do not know, and a
              guessed number is worse than none. When it is there it is the
              single most useful thing a cleaner has for pricing. */}
          <p className="mt-1 text-xs text-slate-500">
            Helps cleaners price it. Leave blank if you&rsquo;re not sure.
          </p>
        </div>
      </div>

      {/* An Airbnb export is all-day — "the guest leaves on the 7th", with no
          hour — but urgency is measured in hours. These are what a synced
          turnover uses, so they are the house's policy rather than a guess. */}
      <div className="grid gap-4 sm:grid-cols-2">
        <div>
          <label htmlFor="default_checkout_time" className="field-label">
            Guests usually check out
          </label>
          <input
            id="default_checkout_time"
            type="time"
            value={String(form.default_checkout_time ?? '11:00').slice(0, 5)}
            onChange={update('default_checkout_time')}
            className="field-input"
          />
        </div>
        <div>
          <label htmlFor="default_checkin_time" className="field-label">
            And check in
          </label>
          <input
            id="default_checkin_time"
            type="time"
            value={String(form.default_checkin_time ?? '16:00').slice(0, 5)}
            onChange={update('default_checkin_time')}
            className="field-input"
          />
          <p className="mt-1 text-xs text-slate-500">
            Used when a booking calendar fills in a turnover for you.
          </p>
        </div>
      </div>

      <div>
        <label htmlFor="cleaning_notes" className="field-label">
          Cleaning notes <span className="font-normal text-slate-400">(optional)</span>
        </label>
        <textarea
          id="cleaning_notes"
          rows={3}
          value={form.cleaning_notes ?? ''}
          onChange={update('cleaning_notes')}
          placeholder="Linens in the hall closet. Recycling goes out Tuesdays."
          className="field-input"
        />
      </div>

      <div>
        <label htmlFor="access_notes" className="field-label">
          How does a cleaner get in?
        </label>
        <textarea
          id="access_notes"
          rows={3}
          value={form.access_notes ?? ''}
          onChange={update('access_notes')}
          placeholder="Lockbox on the porch rail, code 4417."
          className="field-input"
        />
        <p className="mt-1 text-xs text-slate-500">
          Only you see this today. Once bidding opens, it goes to the cleaner you
          award the job to — not to everyone who bids.
        </p>
      </div>

      <button type="submit" disabled={submitting} className="btn-primary w-full">
        {submitting ? 'Saving…' : submitLabel}
      </button>
    </form>
  )
}
