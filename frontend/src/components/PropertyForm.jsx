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
  bedrooms: 1,
  bathrooms: '1',
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
