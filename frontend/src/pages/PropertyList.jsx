import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import Alert from '../components/Alert.jsx'
import EmptyState from '../components/EmptyState.jsx'
import { apiFetch } from '../lib/api.js'

export default function PropertyList() {
  const [properties, setProperties] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    apiFetch('/properties')
      .then(setProperties)
      .catch((err) => setError(err.message))
  }, [])

  return (
    <div className="mx-auto max-w-3xl px-4 py-10">
      <div className="flex items-center justify-between gap-4">
        <h1 className="text-2xl font-bold tracking-tight">Your properties</h1>
        <Link to="/properties/new" className="btn-primary">
          Add a property
        </Link>
      </div>

      <div className="mt-6 space-y-4">
        <Alert>{error}</Alert>

        {properties === null && !error && <p className="text-sm text-slate-500">Loading…</p>}

        {properties?.length === 0 && (
          <EmptyState
            title="No properties yet"
            body="Add the rental you need cleaned. You can post turnovers for it once it's here."
            actionLabel="Add a property"
            actionTo="/properties/new"
          />
        )}

        {properties?.map((property) => (
          <Link
            key={property.id}
            to={`/properties/${property.id}`}
            className="card block transition hover:border-brand-300 hover:shadow"
          >
            <div className="flex items-start justify-between gap-4">
              <div>
                <h2 className="font-semibold">{property.nickname}</h2>
                <p className="mt-1 text-sm text-slate-600">
                  {property.address_line1}
                  {property.address_line2 ? `, ${property.address_line2}` : ''} ·{' '}
                  {property.city}, {property.state} {property.postal_code}
                </p>
              </div>
              <p className="shrink-0 text-sm text-slate-500">
                {property.bedrooms} bd · {Number(property.bathrooms)} ba
              </p>
            </div>
          </Link>
        ))}
      </div>
    </div>
  )
}
