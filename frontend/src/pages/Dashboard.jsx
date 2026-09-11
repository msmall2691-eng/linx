import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import StatusBadge from '../components/StatusBadge.jsx'
import UrgencyBadge from '../components/UrgencyBadge.jsx'
import { apiFetch } from '../lib/api.js'
import { useAuth } from '../lib/auth.jsx'
import { useTimeZone } from '../lib/config.jsx'
import { formatDateTime } from '../lib/datetime.js'
import { showsUrgency } from '../lib/turnover.js'

// What each role will find here once its phase lands. Written out rather than
// left blank so the next phase has an explicit target, and so nobody ships a
// screen that quietly drops one of these.
const NEXT_UP = {
  cleaner: [
    'Finish your profile and set your service area.',
    'Upload your ID and a reference, and clear a background check.',
    'Bid on open turnovers near you once you are cleared.',
  ],
  admin: [
    'Review ID and background checks waiting in the queue.',
    'Watch for turnovers still unclaimed close to checkout.',
    'Handle disputes and reconcile the payment ledger.',
  ],
}

function OwnerDashboard() {
  const timeZone = useTimeZone()
  const [turnovers, setTurnovers] = useState(null)
  const [properties, setProperties] = useState([])

  useEffect(() => {
    Promise.all([apiFetch('/turnovers'), apiFetch('/properties')])
      .then(([loadedTurnovers, loadedProperties]) => {
        setTurnovers(loadedTurnovers)
        setProperties(loadedProperties)
      })
      .catch(() => setTurnovers([]))
  }, [])

  if (turnovers === null) {
    return <p className="mt-6 text-sm text-slate-500">Loading…</p>
  }

  if (properties.length === 0) {
    return (
      <div className="card mt-6">
        <h2 className="font-semibold">Start with a property</h2>
        <p className="mt-2 text-sm text-slate-600">
          Add the rental you need cleaned, then post a turnover for it.
        </p>
        <Link to="/properties/new" className="btn-primary mt-4">
          Add a property
        </Link>
      </div>
    )
  }

  // Soonest first, which the API already orders — the top of that list is what
  // an owner is about to have a problem with.
  const upcoming = turnovers.slice(0, 5)

  return (
    <div className="mt-6 space-y-6">
      <div className="flex flex-wrap gap-3">
        <Link to="/turnovers/new" className="btn-primary">
          Post a turnover
        </Link>
        <Link to="/properties" className="btn-secondary">
          Your properties ({properties.length})
        </Link>
      </div>

      <div>
        <div className="flex items-center justify-between">
          <h2 className="font-semibold">Coming up</h2>
          {turnovers.length > upcoming.length && (
            <Link to="/turnovers" className="text-sm text-brand-600 hover:underline">
              See all {turnovers.length}
            </Link>
          )}
        </div>

        {upcoming.length === 0 ? (
          <p className="mt-2 text-sm text-slate-600">
            Nothing on the schedule.{' '}
            <Link to="/turnovers/new" className="font-medium text-brand-600 hover:underline">
              Post a turnover
            </Link>
            .
          </p>
        ) : (
          <ul className="mt-3 space-y-2">
            {upcoming.map((turnover) => (
              <li key={turnover.id}>
                <Link
                  to={`/turnovers/${turnover.id}`}
                  className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-slate-200 bg-white px-4 py-3 text-sm transition hover:border-brand-300"
                >
                  <span>
                    {properties.find((p) => p.id === turnover.property_id)?.nickname ??
                      'Property'}{' '}
                    <span className="text-slate-500">
                      · {formatDateTime(turnover.checkout_at, timeZone)}
                    </span>
                  </span>
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
      </div>
    </div>
  )
}

function ComingSoon({ steps }) {
  return (
    <>
      <ul className="card mt-6 space-y-3">
        {steps.map((step) => (
          <li key={step} className="flex gap-3 text-sm text-slate-700">
            <span
              aria-hidden="true"
              className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-brand-500"
            />
            {step}
          </li>
        ))}
      </ul>
      <p className="mt-6 text-xs text-slate-500">
        These screens are still being built.
      </p>
    </>
  )
}

export default function Dashboard() {
  const { user } = useAuth()

  return (
    <div className="mx-auto max-w-3xl px-4 py-12">
      <h1 className="text-2xl font-bold tracking-tight">
        Welcome, {user.full_name.split(' ')[0]}
      </h1>

      {user.role === 'owner' ? (
        <OwnerDashboard />
      ) : (
        <>
          <p className="mt-1 text-slate-600">Here is what comes next.</p>
          <ComingSoon steps={NEXT_UP[user.role] ?? []} />
        </>
      )}
    </div>
  )
}
