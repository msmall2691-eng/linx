import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import Alert from '../components/Alert.jsx'
import PaymentPanel from '../components/PaymentPanel.jsx'
import StatusBadge from '../components/StatusBadge.jsx'
import UrgencyBadge from '../components/UrgencyBadge.jsx'
import { apiFetch } from '../lib/api.js'
import { useTimeZone } from '../lib/config.jsx'
import { formatCents, formatDateTime, formatTurnaround } from '../lib/datetime.js'
import { showsUrgency } from '../lib/turnover.js'

const CANCELLABLE = ['draft', 'open', 'awarded']
// Mirrors awards.LIVE_BOOKING_STATUSES on the server. A no-show is only a
// thing while somebody is still on the hook: once the job is marked done it is
// a dispute and a refund, not a no-show, and rendering the button anyway means
// a button that 409s — the dead-screen class this suite exists to catch,
// inverted.
const LIVE_BOOKING = ['awarded', 'in_progress']

/**
 * The bids on this job, cheapest first.
 *
 * Accepting is the one action in the product that hands work to exactly one
 * person, so the server does it under a row lock and this screen does not try
 * to be clever about it: it sends the click and replaces its state with the
 * answer. A second tab that got there first comes back as a plain conflict
 * message, not a silently ignored click.
 */
function Bids({ bids, awarded, onAccept, onDecline, busyBidId }) {
  if (bids === null) {
    return <p className="mt-2 text-sm text-slate-500">Loading bids…</p>
  }

  if (bids.length === 0) {
    return (
      <p className="mt-2 text-sm text-slate-600">
        No bids yet. Cleaners within range of this property can see it on their board.
      </p>
    )
  }

  return (
    <ul className="mt-4 space-y-3" data-testid="bid-list">
      {bids.map((bid) => (
        <li
          key={bid.id}
          data-testid="bid"
          className={`rounded-lg border p-4 ${
            bid.status === 'accepted'
              ? 'border-brand-300 bg-brand-50'
              : 'border-slate-200 bg-white'
          }`}
        >
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <p className="font-medium">{bid.cleaner.full_name}</p>
              <p className="text-sm text-slate-600">
                <strong data-testid="bid-price">{formatCents(bid.price_cents)}</strong> ·{' '}
                <span data-testid="bid-status">{bid.status}</span>
              </p>
              {bid.message && (
                <p className="mt-2 whitespace-pre-wrap text-sm text-slate-700">{bid.message}</p>
              )}
              <p className="mt-2 text-xs text-slate-500">
                {bid.cleaner.can_take_jobs ? 'Vetting complete' : 'Vetting not finished'}
                {' · '}
                {bid.cleaner.has_insurance_on_file
                  ? 'Insurance on file'
                  : 'No insurance on file'}
              </p>
            </div>

            {!awarded && bid.status === 'submitted' && (
              <div className="flex gap-2">
                <button
                  type="button"
                  disabled={busyBidId !== null || !bid.cleaner.can_take_jobs}
                  onClick={() => onAccept(bid.id)}
                  className="btn-primary disabled:opacity-50"
                  data-testid="accept-bid"
                >
                  {busyBidId === bid.id ? 'Accepting…' : 'Accept'}
                </button>
                <button
                  type="button"
                  disabled={busyBidId !== null}
                  onClick={() => onDecline(bid.id)}
                  className="btn-secondary disabled:opacity-50"
                >
                  Decline
                </button>
              </div>
            )}
          </div>

          {!bid.cleaner.can_take_jobs && bid.status === 'submitted' && (
            <p className="mt-2 text-xs text-slate-500">
              This cleaner cannot be accepted until their vetting is finished.
            </p>
          )}
        </li>
      ))}
    </ul>
  )
}

export default function TurnoverDetail() {
  const { turnoverId } = useParams()
  const timeZone = useTimeZone()

  const [turnover, setTurnover] = useState(null)
  const [bids, setBids] = useState(null)
  const [error, setError] = useState(null)
  const [actionError, setActionError] = useState(null)
  const [confirmingCancel, setConfirmingCancel] = useState(false)
  const [confirmingNoShow, setConfirmingNoShow] = useState(false)
  const [cancelReason, setCancelReason] = useState('')
  const [busyBidId, setBusyBidId] = useState(null)

  useEffect(() => {
    let cancelled = false
    apiFetch(`/turnovers/${turnoverId}`)
      .then((loaded) => !cancelled && setTurnover(loaded))
      .catch((err) => !cancelled && setError(err.message))
    apiFetch(`/turnovers/${turnoverId}/bids`)
      .then((loaded) => !cancelled && setBids(loaded))
      .catch((err) => !cancelled && setActionError(err.message))
    return () => {
      cancelled = true
    }
  }, [turnoverId])

  async function runAction(path, body) {
    setActionError(null)
    try {
      // The action answers with the same shape the GET did, so replacing state
      // with it is safe. An action that answered with less would blank this
      // page on the next render — that has happened here before.
      setTurnover(await apiFetch(path, { method: 'POST', body }))
      setConfirmingCancel(false)
      setConfirmingNoShow(false)
      setBids(await apiFetch(`/turnovers/${turnoverId}/bids`))
    } catch (err) {
      setActionError(err.message)
    }
  }

  async function acceptBid(bidId) {
    setBusyBidId(bidId)
    try {
      await runAction(`/turnovers/${turnoverId}/bids/${bidId}/accept`)
    } finally {
      setBusyBidId(null)
    }
  }

  async function declineBid(bidId) {
    setActionError(null)
    setBusyBidId(bidId)
    try {
      setBids(
        await apiFetch(`/turnovers/${turnoverId}/bids/${bidId}/decline`, { method: 'POST' }),
      )
    } catch (err) {
      setActionError(err.message)
    } finally {
      setBusyBidId(null)
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

      {turnover.award && (
        <PaymentPanel
          turnoverId={turnoverId}
          award={turnover.award}
          status={turnover.status}
        />
      )}

      {turnover.award && (
        <div
          className="mt-6 rounded-xl border border-brand-200 bg-brand-50 p-6"
          data-testid="award-panel"
        >
          <h2 className="font-semibold">Booked</h2>
          <p className="mt-2 text-sm text-slate-700">
            <strong data-testid="award-cleaner">{turnover.award.cleaner_name}</strong> is
            cleaning this turnover for{' '}
            <strong data-testid="award-price">
              {formatCents(turnover.award.agreed_price_cents)}
            </strong>
            . They have the address and your access notes.
          </p>
          <p className="mt-1 text-xs text-slate-500">
            Accepted {formatDateTime(turnover.award.awarded_at, timeZone)}
          </p>

          <div className="mt-4 flex flex-wrap gap-2">
            {confirmingNoShow ? (
              <div className="w-full space-y-3">
                <label htmlFor="no-show-reason" className="field-label">
                  What happened?
                </label>
                <textarea
                  id="no-show-reason"
                  rows={2}
                  value={cancelReason}
                  onChange={(e) => setCancelReason(e.target.value)}
                  placeholder="Nobody arrived and there was no message."
                  className="field-input"
                />
                <div className="flex gap-2">
                  <button
                    type="button"
                    disabled={!cancelReason.trim()}
                    onClick={() =>
                      runAction(`/turnovers/${turnoverId}/no-show`, { reason: cancelReason })
                    }
                    className="btn-primary bg-red-600 hover:bg-red-700 disabled:opacity-50"
                  >
                    Report the no-show
                  </button>
                  <button
                    type="button"
                    onClick={() => setConfirmingNoShow(false)}
                    className="btn-secondary"
                  >
                    Never mind
                  </button>
                </div>
                <p className="text-xs text-slate-500">
                  The job goes straight back to the bench, and we tell the cleaner and an
                  admin.
                </p>
              </div>
            ) : (
              LIVE_BOOKING.includes(turnover.status) && (
                <button
                  type="button"
                  onClick={() => {
                    setCancelReason('')
                    setConfirmingNoShow(true)
                  }}
                  className="btn-secondary text-red-700"
                  data-testid="report-no-show"
                >
                  They did not turn up
                </button>
              )
            )}
          </div>
        </div>
      )}

      {['open', 'awarded'].includes(turnover.status) && (
        <div className="mt-6 rounded-xl border border-slate-200 bg-white p-6">
          <h2 className="font-semibold">Bids</h2>
          {turnover.status === 'open' && (
            <p className="mt-1 text-sm text-slate-600">
              Cleaners near this property can see the job and name a price.
            </p>
          )}
          <Bids
            bids={bids}
            awarded={Boolean(turnover.award)}
            onAccept={acceptBid}
            onDecline={declineBid}
            busyBidId={busyBidId}
          />
        </div>
      )}

      {CANCELLABLE.includes(turnover.status) && (
        <div className="mt-12 border-t border-slate-200 pt-6">
          {confirmingCancel ? (
            <div className="space-y-3">
              <label htmlFor="reason" className="field-label">
                Why are you cancelling?{' '}
                {turnover.award ? (
                  <span className="font-normal text-slate-500">
                    — the cleaner is told what you write here
                  </span>
                ) : (
                  <span className="font-normal text-slate-400">(optional)</span>
                )}
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
                  // A booked cleaner has arranged their day around this, so the
                  // server insists on a reason. Mirrored here so the refusal is
                  // a disabled button rather than a bounced request.
                  disabled={Boolean(turnover.award) && !cancelReason.trim()}
                  onClick={() =>
                    runAction(`/turnovers/${turnoverId}/cancel`, {
                      reason: cancelReason || null,
                    })
                  }
                  className="btn-primary bg-red-600 hover:bg-red-700 disabled:opacity-50"
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
