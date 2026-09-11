import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import Alert from '../components/Alert.jsx'
import StatusBadge from '../components/StatusBadge.jsx'
import UrgencyBadge from '../components/UrgencyBadge.jsx'
import { apiFetch } from '../lib/api.js'
import { useTimeZone } from '../lib/config.jsx'
import { formatCents, formatDateTime, formatTurnaround } from '../lib/datetime.js'
import { showsUrgency } from '../lib/turnover.js'

const CANCELLABLE = ['draft', 'open']

export default function TurnoverDetail() {
  const { turnoverId } = useParams()
  const timeZone = useTimeZone()

  const [turnover, setTurnover] = useState(null)
  const [error, setError] = useState(null)
  const [actionError, setActionError] = useState(null)
  const [confirmingCancel, setConfirmingCancel] = useState(false)
  const [cancelReason, setCancelReason] = useState('')

  useEffect(() => {
    let cancelled = false
    apiFetch(`/turnovers/${turnoverId}`)
      .then((loaded) => !cancelled && setTurnover(loaded))
      .catch((err) => !cancelled && setError(err.message))
    return () => {
      cancelled = true
    }
  }, [turnoverId])

  async function runAction(path, body) {
    setActionError(null)
    try {
      setTurnover(await apiFetch(path, { method: 'POST', body }))
      setConfirmingCancel(false)
    } catch (err) {
      setActionError(err.message)
    }
  }

  if (error) {
    return (
      <div className="mx-auto max-w-2xl px-4 py-10">
        <Alert>{error}</Alert>
      </div>
    )
  }

  if (!turnover) {
    return <p className="mx-auto max-w-2xl px-4 py-10 text-sm text-slate-500">Loading…</p>
  }

  const property = turnover.property

  return (
    <div className="mx-auto max-w-3xl px-4 py-10">
      <Link to="/turnovers" className="text-sm text-brand-600 hover:underline">
        ← Turnovers
      </Link>

      <div className="mt-2 flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">{property.nickname}</h1>
          <Link
            to={`/properties/${property.id}`}
            className="mt-1 block text-sm text-slate-600 hover:underline"
          >
            {property.address_line1}, {property.city}, {property.state}
          </Link>
        </div>
        <div className="flex gap-2">
          {showsUrgency(turnover.status) && <UrgencyBadge urgency={turnover.urgency} />}
          <StatusBadge status={turnover.status} />
        </div>
      </div>

      <div className="mt-4">
        <Alert>{actionError}</Alert>
      </div>

      <dl className="card mt-6 grid gap-4 sm:grid-cols-2">
        <div>
          <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">
            Guest checks out
          </dt>
          <dd className="mt-1 text-sm font-medium">
            {formatDateTime(turnover.checkout_at, timeZone)}
          </dd>
        </div>
        <div>
          <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">
            Next guest checks in
          </dt>
          <dd className="mt-1 text-sm font-medium">
            {turnover.checkin_at ? (
              formatDateTime(turnover.checkin_at, timeZone)
            ) : (
              <span className="text-slate-400">None booked yet</span>
            )}
          </dd>
        </div>
        <div className="sm:col-span-2">
          <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">
            The window
          </dt>
          <dd className="mt-1 text-sm">
            {formatTurnaround(turnover.checkout_at, turnover.checkin_at)}
            {turnover.is_same_day && showsUrgency(turnover.status) && (
              <span className="ml-2 font-medium text-urgency-urgent">
                Same-day turnaround
              </span>
            )}
          </dd>
        </div>
        <div>
          <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">
            Your budget
          </dt>
          <dd className="mt-1 text-sm">{formatCents(turnover.owner_budget_cents)}</dd>
        </div>
        <div>
          <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">Posted</dt>
          <dd className="mt-1 text-sm">{formatDateTime(turnover.created_at, timeZone)}</dd>
        </div>
        {turnover.notes && (
          <div className="sm:col-span-2">
            <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">Notes</dt>
            <dd className="mt-1 whitespace-pre-wrap text-sm">{turnover.notes}</dd>
          </div>
        )}
        {turnover.cancelled_at && (
          <div className="sm:col-span-2">
            <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">
              Cancelled
            </dt>
            <dd className="mt-1 text-sm">
              {formatDateTime(turnover.cancelled_at, timeZone)}
              {turnover.cancellation_reason ? ` — ${turnover.cancellation_reason}` : ''}
            </dd>
          </div>
        )}
      </dl>

      {turnover.status === 'draft' && (
        <div className="mt-6 flex flex-wrap items-center gap-3">
          <button
            type="button"
            onClick={() => runAction(`/turnovers/${turnoverId}/publish`)}
            className="btn-primary"
          >
            Post it to cleaners
          </button>
          <p className="text-sm text-slate-500">
            Nobody can see this draft yet.
          </p>
        </div>
      )}

      {turnover.status === 'open' && (
        <div className="mt-6 rounded-xl border border-slate-200 bg-white p-6">
          <h2 className="font-semibold">Bids</h2>
          <p className="mt-2 text-sm text-slate-600">
            Cleaners near this property can see the job and name a price. Bidding is
            still being built — bids will show up here.
          </p>
        </div>
      )}

      {CANCELLABLE.includes(turnover.status) && (
        <div className="mt-12 border-t border-slate-200 pt-6">
          {confirmingCancel ? (
            <div className="space-y-3">
              <label htmlFor="reason" className="field-label">
                Why are you cancelling?{' '}
                <span className="font-normal text-slate-400">(optional)</span>
              </label>
              <textarea
                id="reason"
                rows={2}
                value={cancelReason}
                onChange={(e) => setCancelReason(e.target.value)}
                placeholder="Guest extended their stay."
                className="field-input"
              />
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() =>
                    runAction(`/turnovers/${turnoverId}/cancel`, {
                      reason: cancelReason || null,
                    })
                  }
                  className="btn-primary bg-red-600 hover:bg-red-700"
                >
                  Cancel this turnover
                </button>
                <button
                  type="button"
                  onClick={() => setConfirmingCancel(false)}
                  className="btn-secondary"
                >
                  Keep it
                </button>
              </div>
            </div>
          ) : (
            <button
              type="button"
              onClick={() => setConfirmingCancel(true)}
              className="btn-secondary text-red-700"
            >
              Cancel this turnover
            </button>
          )}
        </div>
      )}
    </div>
  )
}
