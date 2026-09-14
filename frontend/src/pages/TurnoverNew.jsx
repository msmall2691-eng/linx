import { useEffect, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'

import Alert from '../components/Alert.jsx'
import UrgencyBadge from '../components/UrgencyBadge.jsx'
import { apiFetch } from '../lib/api.js'
import { useConfig, useTimeZone } from '../lib/config.jsx'
import { dollarsToCents, formatTurnaround, zonedInputToISO } from '../lib/datetime.js'

// What a home's clean can be. A rental's is always a turnover, so it is not
// offered — the server refuses a mismatch rather than correcting it, and a form
// that offers an option the API rejects is a form that lies.
const HOME_SCOPES = [
  { value: 'standard', label: 'Standard clean', hint: 'The regular going-over.' },
  { value: 'deep', label: 'Deep clean', hint: 'Inside the oven, behind things, the works.' },
  { value: 'move_out', label: 'Move-out clean', hint: 'Empty, and being handed back.' },
]

/**
 * A local preview of the urgency ladder, so the owner sees what posting these
 * dates means before they commit.
 *
 * The server decides the real rung (app/services/urgency.py) and its answer is
 * what comes back on the created turnover — this never writes anything and is
 * never read back as truth. It exists so the ladder is visible while the owner
 * is still choosing the times, which is the only moment they can change it.
 */
function previewUrgency(checkoutISO, checkinISO, timeZone) {
  if (!checkoutISO) return null

  const checkout = new Date(checkoutISO)
  if (checkinISO) {
    const checkin = new Date(checkinISO)
    const sameDay =
      checkout.toLocaleDateString('en-US', { timeZone }) ===
      checkin.toLocaleDateString('en-US', { timeZone })
    if (sameDay) return 'same_day'
    const hours = (checkin - checkout) / 36e5
    if (hours < 24) return 'urgent'
    if (hours < 72) return 'soon'
    return 'standard'
  }

  const hours = (checkout - new Date()) / 36e5
  if (hours < 24) return 'urgent'
  if (hours < 72) return 'soon'
  return 'standard'
}

export default function TurnoverNew() {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const timeZone = useTimeZone()
  const { region_name: regionName } = useConfig()

  const [properties, setProperties] = useState([])
  const [form, setForm] = useState({
    property_id: searchParams.get('property') ?? '',
    checkout_at: '',
    checkin_at: '',
    service_type: '',
    budget: '',
    notes: '',
    publish: true,
  })
  const [error, setError] = useState(null)
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    apiFetch('/properties')
      .then((loaded) => {
        setProperties(loaded)
        setForm((prev) =>
          prev.property_id || loaded.length === 0
            ? prev
            : { ...prev, property_id: loaded[0].id },
        )
      })
      .catch((err) => setError(err.message))
  }, [])

  function update(field) {
    return (event) =>
      setForm((prev) => ({
        ...prev,
        [field]: event.target.type === 'checkbox' ? event.target.checked : event.target.value,
      }))
  }

  // Which property this is for decides what the form even asks. A home has no
  // next guest, so there is no checkin field to leave blank and nothing to
  // measure a window against.
  const property = properties.find((p) => p.id === form.property_id)
  const isHome = property?.property_type === 'residential'

  const checkoutISO = zonedInputToISO(form.checkout_at, timeZone)
  const checkinISO = isHome ? null : zonedInputToISO(form.checkin_at, timeZone)
  const preview = previewUrgency(checkoutISO, checkinISO, timeZone)

  async function handleSubmit(event) {
    event.preventDefault()
    setError(null)

    const budgetCents = dollarsToCents(form.budget)
    if (form.budget !== '' && budgetCents === null) {
      setError('Enter the budget as dollars and cents, like 125 or 125.50.')
      return
    }

    setSubmitting(true)
    try {
      const created = await apiFetch('/turnovers', {
        method: 'POST',
        body: {
          property_id: form.property_id,
          checkout_at: checkoutISO,
          checkin_at: checkinISO,
          // Omitted on a rental, where there is exactly one right answer and
          // the server supplies it.
          service_type: isHome ? form.service_type || 'standard' : undefined,
          owner_budget_cents: budgetCents,
          notes: form.notes || null,
          publish: form.publish,
        },
      })
      navigate(`/turnovers/${created.id}`, { replace: true })
    } catch (err) {
      setError(err.message)
    } finally {
      setSubmitting(false)
    }
  }

  if (properties.length === 0) {
    return (
      <div className="mx-auto max-w-2xl px-4 py-10">
        <h1 className="text-2xl font-bold tracking-tight">Post a turnover</h1>
        <p className="mt-3 text-slate-600">
          Add a property first — a job is a cleaning at one of your places.
        </p>
        <Link to="/properties/new" className="btn-primary mt-6">
          Add a property
        </Link>
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-2xl px-4 py-10">
      <Link to="/turnovers" className="text-sm text-brand-600 hover:underline">
        ← Turnovers
      </Link>
      <h1 className="mt-2 text-2xl font-bold tracking-tight">
        {isHome ? 'Post a clean' : 'Post a turnover'}
      </h1>
      <p className="mt-1 text-sm text-slate-600">
        Times are {regionName} local.
      </p>

      <form onSubmit={handleSubmit} className="card mt-6 space-y-4">
        <Alert>{error}</Alert>

        <div>
          <label htmlFor="property_id" className="field-label">
            Property
          </label>
          <select
            id="property_id"
            required
            value={form.property_id}
            onChange={update('property_id')}
            className="field-input"
          >
            {properties.map((property) => (
              <option key={property.id} value={property.id}>
                {property.nickname}
                {property.property_type === 'residential' ? ' (home)' : ''}
              </option>
            ))}
          </select>
        </div>

        {isHome && (
          <fieldset>
            <legend className="field-label">What kind of clean?</legend>
            <div className="mt-2 space-y-2">
              {HOME_SCOPES.map((scope) => (
                <label
                  key={scope.value}
                  className={`flex cursor-pointer gap-3 rounded-lg border p-3 text-sm transition ${
                    (form.service_type || 'standard') === scope.value
                      ? 'border-brand-500 bg-brand-50 ring-1 ring-brand-500'
                      : 'border-slate-300 hover:bg-slate-50'
                  }`}
                >
                  <input
                    type="radio"
                    name="service_type"
                    value={scope.value}
                    checked={(form.service_type || 'standard') === scope.value}
                    onChange={update('service_type')}
                    className="sr-only"
                  />
                  <span>
                    <span className="block font-medium">{scope.label}</span>
                    <span className="mt-0.5 block text-xs text-slate-500">{scope.hint}</span>
                  </span>
                </label>
              ))}
            </div>
          </fieldset>
        )}

        <div>
          <label htmlFor="checkout_at" className="field-label">
            {isHome ? 'When should it be cleaned?' : 'Guest checks out'}
          </label>
          <input
            id="checkout_at"
            type="datetime-local"
            required
            value={form.checkout_at}
            onChange={update('checkout_at')}
            className="field-input"
          />
        </div>

        {/* A home has no next guest. The field is not disabled or ignored —
            it is absent, because a checkin on a home is a category error and
            the server refuses one rather than dropping it. */}
        {!isHome && (
          <div>
            <label htmlFor="checkin_at" className="field-label">
              Next guest checks in{' '}
              <span className="font-normal text-slate-400">(leave blank if none booked)</span>
            </label>
            <input
              id="checkin_at"
              type="datetime-local"
              min={form.checkout_at || undefined}
              value={form.checkin_at}
              onChange={update('checkin_at')}
              className="field-input"
            />
          </div>
        )}

        {preview && (
          <div className="flex items-center gap-3 rounded-lg bg-slate-50 px-3 py-2.5">
            <UrgencyBadge urgency={preview} />
            <span className="text-sm text-slate-600">
              {formatTurnaround(checkoutISO, checkinISO)}
            </span>
          </div>
        )}

        <div>
          <label htmlFor="budget" className="field-label">
            What you expect to pay{' '}
            <span className="font-normal text-slate-400">(optional)</span>
          </label>
          <div className="relative mt-1">
            <span className="pointer-events-none absolute inset-y-0 left-3 flex items-center text-sm text-slate-400">
              $
            </span>
            <input
              id="budget"
              inputMode="decimal"
              value={form.budget}
              onChange={update('budget')}
              placeholder="125"
              className="field-input mt-0 pl-7"
            />
          </div>
          <p className="mt-1 text-xs text-slate-500">
            A guide for cleaners. They still name their own price.
          </p>
        </div>

        <div>
          <label htmlFor="notes" className="field-label">
            Anything specific to this turnover{' '}
            <span className="font-normal text-slate-400">(optional)</span>
          </label>
          <textarea
            id="notes"
            rows={3}
            value={form.notes}
            onChange={update('notes')}
            placeholder="Guests had a dog. Extra vacuuming on the rugs, please."
            className="field-input"
          />
        </div>

        <label className="flex items-start gap-2 text-sm text-slate-700">
          <input
            type="checkbox"
            checked={form.publish}
            onChange={update('publish')}
            className="mt-0.5 rounded border-slate-300 text-brand-600 focus:ring-brand-500"
          />
          <span>
            Post it to cleaners now
            <span className="block text-xs text-slate-500">
              Uncheck to save it as a draft you can post later.
            </span>
          </span>
        </label>

        <button type="submit" disabled={submitting} className="btn-primary w-full">
          {submitting ? 'Posting…' : form.publish ? 'Post turnover' : 'Save draft'}
        </button>
      </form>
    </div>
  )
}
