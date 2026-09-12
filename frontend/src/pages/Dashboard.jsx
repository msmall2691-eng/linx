import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import StatusBadge from '../components/StatusBadge.jsx'
import UrgencyBadge from '../components/UrgencyBadge.jsx'
import VettingPanel from '../components/VettingPanel.jsx'
import { apiFetch } from '../lib/api.js'
import { useAuth } from '../lib/auth.jsx'
import { useTimeZone } from '../lib/config.jsx'
import { formatDateTime } from '../lib/datetime.js'
import { showsUrgency } from '../lib/turnover.js'

// What each role will find here once its phase lands. Written out rather than
// left blank so the next phase has an explicit target, and so nobody ships a
// screen that quietly drops one of these.
const NEXT_UP = {
  admin: [
    'Watch for turnovers still unclaimed close to checkout.',
    'Handle disputes and reconcile the payment ledger.',
  ],
}

function CleanerDashboard() {
  const [vetting, setVetting] = useState(null)
  const [openCount, setOpenCount] = useState(null)

  useEffect(() => {
    apiFetch('/cleaner/profile')
      .then((profile) => setVetting(profile.vetting))
      .catch(() => setVetting(null))
    apiFetch('/board')
      .then((board) => setOpenCount(board.length))
      .catch(() => setOpenCount(null))
  }, [])

  return (
    <div className="mt-6 space-y-6">
      {vetting ? (
        <VettingPanel vetting={vetting} />
      ) : (
        <div className="card">
          <h2 className="font-semibold">Set up your profile</h2>
          <p className="mt-2 text-sm text-slate-600">
            Tell us where you work, then upload an ID and a reference so a person can
            review them.
          </p>
          <Link to="/cleaner/profile" className="btn-primary mt-4">
            Set up your profile
          </Link>
        </div>
      )}

      <div className="flex flex-wrap gap-3">
        <Link to="/board" className="btn-primary">
          {openCount === null
            ? 'Open turnovers'
            : `Open turnovers near you (${openCount})`}
        </Link>
        <Link to="/cleaner/profile" className="btn-secondary">
          Your profile
        </Link>
      </div>
    </div>
  )
}

function AdminDashboard() {
  const [waiting, setWaiting] = useState(null)

  useEffect(() => {
    apiFetch('/admin/vetting-queue')
      .then((queue) => setWaiting(queue.length))
      .catch(() => setWaiting(null))
  }, [])

  return (
    <div className="mt-6 space-y-6">
      <div className="card">
        <h2 className="font-semibold">Vetting queue</h2>
        <p className="mt-2 text-sm text-slate-600">
          {waiting === null
            ? 'Cleaners waiting on a review.'
            : waiting === 0
              ? 'Nobody is waiting on a review right now.'
              : `${waiting} cleaner${waiting === 1 ? '' : 's'} waiting on a review. A 1–2 day turnaround only holds if the queue gets worked.`}
        </p>
        <Link to="/admin/vetting" className="btn-primary mt-4">
          Open the queue
        </Link>
      </div>

      <ComingSoon steps={NEXT_UP.admin} />
    </div>
  )
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

      {user.role === 'owner' && <OwnerDashboard />}
      {user.role === 'cleaner' && <CleanerDashboard />}
      {user.role === 'admin' && <AdminDashboard />}
    </div>
  )
}
