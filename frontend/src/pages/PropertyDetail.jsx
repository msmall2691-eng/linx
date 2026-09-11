import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import Alert from '../components/Alert.jsx'
import PropertyForm from '../components/PropertyForm.jsx'
import StatusBadge from '../components/StatusBadge.jsx'
import UrgencyBadge from '../components/UrgencyBadge.jsx'
import { apiFetch } from '../lib/api.js'
import { useTimeZone } from '../lib/config.jsx'
import { formatDateTime } from '../lib/datetime.js'
import { showsUrgency } from '../lib/turnover.js'

export default function PropertyDetail() {
  const { propertyId } = useParams()
  const navigate = useNavigate()
  const timeZone = useTimeZone()

  const [property, setProperty] = useState(null)
  const [turnovers, setTurnovers] = useState([])
  const [editing, setEditing] = useState(false)
  const [error, setError] = useState(null)
  const [formError, setFormError] = useState(null)

  useEffect(() => {
    let cancelled = false
    Promise.all([
      apiFetch(`/properties/${propertyId}`),
      apiFetch(`/turnovers?property_id=${propertyId}`),
    ])
      .then(([loadedProperty, loadedTurnovers]) => {
        if (cancelled) return
        setProperty(loadedProperty)
        setTurnovers(loadedTurnovers)
      })
      .catch((err) => !cancelled && setError(err.message))
    return () => {
      cancelled = true
    }
  }, [propertyId])

  async function handleSave(payload) {
    setFormError(null)
    try {
      setProperty(await apiFetch(`/properties/${propertyId}`, { method: 'PATCH', body: payload }))
      setEditing(false)
    } catch (err) {
      setFormError(err.message)
    }
  }

  async function handleArchive() {
    setError(null)
    try {
      await apiFetch(`/properties/${propertyId}`, { method: 'DELETE' })
      navigate('/properties', { replace: true })
    } catch (err) {
      // A property with turnovers still on the schedule refuses to archive, and
      // says why. Surfacing that is the point — the alternative is a property
      // quietly vanishing while a cleaner is still booked to show up.
      setError(err.message)
    }
  }

  if (error && !property) {
    return (
      <div className="mx-auto max-w-2xl px-4 py-10">
        <Alert>{error}</Alert>
      </div>
    )
  }

  if (!property) {
    return <p className="mx-auto max-w-2xl px-4 py-10 text-sm text-slate-500">Loading…</p>
  }

  return (
    <div className="mx-auto max-w-3xl px-4 py-10">
      <Link to="/properties" className="text-sm text-brand-600 hover:underline">
        ← Your properties
      </Link>

      <div className="mt-2 flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">{property.nickname}</h1>
          <p className="mt-1 text-sm text-slate-600">
            {property.address_line1}
            {property.address_line2 ? `, ${property.address_line2}` : ''}, {property.city},{' '}
            {property.state} {property.postal_code}
          </p>
          {!property.is_active && (
            <p className="mt-2 text-sm font-medium text-slate-500">Archived</p>
          )}
        </div>
        <div className="flex gap-2">
          <button type="button" onClick={() => setEditing((v) => !v)} className="btn-secondary">
            {editing ? 'Cancel' : 'Edit'}
          </button>
          <Link to={`/turnovers/new?property=${property.id}`} className="btn-primary">
            Post a turnover
          </Link>
        </div>
      </div>

      <div className="mt-4">
        <Alert>{error}</Alert>
      </div>

      {editing ? (
        <PropertyForm
          initial={property}
          onSubmit={handleSave}
          submitLabel="Save changes"
          error={formError}
        />
      ) : (
        <>
          <dl className="card mt-6 grid gap-4 sm:grid-cols-2">
            <div>
              <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">Size</dt>
              <dd className="mt-1 text-sm">
                {property.bedrooms} bedroom{property.bedrooms === 1 ? '' : 's'} ·{' '}
                {Number(property.bathrooms)} bath
              </dd>
            </div>
            <div>
              <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">
                Access
              </dt>
              <dd className="mt-1 whitespace-pre-wrap text-sm">
                {property.access_notes || <span className="text-slate-400">Not set</span>}
              </dd>
            </div>
            <div className="sm:col-span-2">
              <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">
                Cleaning notes
              </dt>
              <dd className="mt-1 whitespace-pre-wrap text-sm">
                {property.cleaning_notes || <span className="text-slate-400">Not set</span>}
              </dd>
            </div>
          </dl>

          <h2 className="mt-10 font-semibold">Turnovers</h2>
          {turnovers.length === 0 ? (
            <p className="mt-2 text-sm text-slate-600">
              Nothing scheduled here yet.{' '}
              <Link
                to={`/turnovers/new?property=${property.id}`}
                className="font-medium text-brand-600 hover:underline"
              >
                Post one
              </Link>
              .
            </p>
          ) : (
            <ul className="mt-3 space-y-2">
              {turnovers.map((turnover) => (
                <li key={turnover.id}>
                  <Link
                    to={`/turnovers/${turnover.id}`}
                    className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-slate-200 bg-white px-4 py-3 text-sm transition hover:border-brand-300"
                  >
                    <span>{formatDateTime(turnover.checkout_at, timeZone)}</span>
                    <span className="flex gap-2">
                      {showsUrgency(turnover.status) && (
                        <UrgencyBadge urgency={turnover.urgency} />
                      )}
                      <StatusBadge status={turnover.status} />
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          )}

          {property.is_active && (
            <div className="mt-12 border-t border-slate-200 pt-6">
              <button type="button" onClick={handleArchive} className="btn-secondary text-red-700">
                Archive this property
              </button>
              <p className="mt-2 text-xs text-slate-500">
                It stops showing in your list. Nothing is deleted, and turnovers already on
                the schedule have to be cancelled first.
              </p>
            </div>
          )}
        </>
      )}
    </div>
  )
}
