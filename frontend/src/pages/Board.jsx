import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import Alert from '../components/Alert.jsx'
import EmptyState from '../components/EmptyState.jsx'
import { PropertySpecs, ScopeBadge } from '../components/JobScope.jsx'
import UrgencyBadge from '../components/UrgencyBadge.jsx'
import { apiFetch } from '../lib/api.js'
import { useTimeZone } from '../lib/config.jsx'
import { dollarsToCents, formatCents, formatDateTime, formatTurnaround } from '../lib/datetime.js'

/**
 * The bench: open turnovers inside the cleaner's radius.
 *
 * Note what isn't here — no street address and no access notes. The API's board
 * shape withholds them, because a cleaner who has bid has not been hired. Those
 * details are released on award.
 */
function BidBox({ turnover, canBid, blockedReason, onBid }) {
  const [price, setPrice] = useState(
    turnover.my_bid ? String(turnover.my_bid.price_cents / 100) : '',
  )
  const [message, setMessage] = useState(turnover.my_bid?.message ?? '')
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  if (!canBid) {
    return (
      <div className="mt-4 border-t border-slate-200 pt-4">
        <Alert tone="warning">{blockedReason}</Alert>
        <Link to="/cleaner/profile" className="btn-secondary mt-3">
          Finish your profile
        </Link>
      </div>
    )
  }

  async function submit(event) {
    event.preventDefault()
    const cents = dollarsToCents(price)
    if (cents === null || cents <= 0) {
      setError('Enter your price as dollars and cents, like 125 or 125.50.')
      return
    }
    setError(null)
    setBusy(true)
    try {
      await onBid(turnover.id, { price_cents: cents, message: message || null })
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const accepted = turnover.my_bid?.status === 'accepted'

  return (
    <form onSubmit={submit} className="mt-4 space-y-3 border-t border-slate-200 pt-4">
      <Alert>{error}</Alert>

      {turnover.my_bid && (
        <p className="text-sm text-slate-600">
          Your bid: <strong>{formatCents(turnover.my_bid.price_cents)}</strong> ·{' '}
          {turnover.my_bid.status}
        </p>
      )}

      {!accepted && (
        <>
          <div className="flex flex-wrap items-end gap-3">
            <div className="min-w-[8rem] flex-1">
              <label htmlFor={`price-${turnover.id}`} className="field-label">
                Your price
              </label>
              <div className="relative mt-1">
                <span className="pointer-events-none absolute inset-y-0 left-3 flex items-center text-sm text-slate-400">
                  $
                </span>
                <input
                  id={`price-${turnover.id}`}
                  inputMode="decimal"
                  value={price}
                  onChange={(e) => setPrice(e.target.value)}
                  placeholder="125"
                  className="field-input mt-0 pl-7"
                />
              </div>
            </div>
            <button type="submit" disabled={busy} className="btn-primary">
              {busy ? 'Sending…' : turnover.my_bid ? 'Update bid' : 'Place bid'}
            </button>
          </div>

          <div>
            <label htmlFor={`message-${turnover.id}`} className="field-label">
              Message <span className="font-normal text-slate-400">(optional)</span>
            </label>
            <textarea
              id={`message-${turnover.id}`}
              rows={2}
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              placeholder="I can be there by 11 and I bring my own supplies."
              className="field-input"
            />
          </div>
        </>
      )}
    </form>
  )
}

export default function Board() {
  const timeZone = useTimeZone()

  const [turnovers, setTurnovers] = useState(null)
  const [vetting, setVetting] = useState(null)
  const [error, setError] = useState(null)

  async function load() {
    const [board, profile] = await Promise.all([
      apiFetch('/board'),
      apiFetch('/cleaner/profile').catch(() => null),
    ])
    setTurnovers(board)
    setVetting(profile?.vetting ?? null)
  }

  useEffect(() => {
    load().catch((err) => {
      if (err.status === 404) {
        setTurnovers([])
        setVetting(null)
      } else {
        setError(err.message)
      }
    })
  }, [])

  async function handleBid(turnoverId, body) {
    await apiFetch(`/board/${turnoverId}/bid`, { method: 'PUT', body })
    await load()
  }

  if (error) {
    return (
      <div className="mx-auto max-w-3xl px-4 py-10">
        <Alert>{error}</Alert>
      </div>
    )
  }

  if (turnovers === null) {
    return <p className="mx-auto max-w-3xl px-4 py-10 text-sm text-slate-500">Loading…</p>
  }

  // The gate and this message come from the same server-side answer, so the
  // board cannot invite a bid the API would refuse.
  const canBid = Boolean(vetting?.can_take_jobs)
  const blockedReason = vetting?.summary ?? 'Set up your cleaner profile to start bidding.'

  return (
    <div className="mx-auto max-w-3xl px-4 py-10">
      <h1 className="text-2xl font-bold tracking-tight">Open turnovers</h1>
      <p className="mt-1 text-sm text-slate-600">
        Jobs inside your service area. Addresses and entry details are shared once a
        job is awarded to you.
      </p>

      {!canBid && (
        <div className="mt-4">
          <Alert tone="warning">{blockedReason}</Alert>
        </div>
      )}

      <div className="mt-6 space-y-4">
        {turnovers.length === 0 && (
          <EmptyState
            title="Nothing open near you right now"
            body="New turnovers appear here as soon as owners post them inside your radius. Widening your radius shows more."
            actionLabel="Adjust your service area"
            actionTo="/cleaner/profile"
          />
        )}

        {turnovers.map((turnover) => (
          <div key={turnover.id} className="card">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <h2 className="font-semibold">{turnover.property.nickname}</h2>
                <p className="mt-1 text-sm text-slate-600">
                  {turnover.property.city}, {turnover.property.state} ·{' '}
                  {turnover.distance_miles} mi away
                </p>
                <PropertySpecs property={turnover.property} />
              </div>
              <div className="flex shrink-0 flex-col items-end gap-2">
                <UrgencyBadge urgency={turnover.urgency} />
                <ScopeBadge serviceType={turnover.service_type} />
              </div>
            </div>

            <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-2">
              <div>
                <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">
                  Checkout
                </dt>
                <dd className="mt-0.5">{formatDateTime(turnover.checkout_at, timeZone)}</dd>
              </div>
              <div>
                <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">
                  Next checkin
                </dt>
                <dd className="mt-0.5">
                  {turnover.checkin_at
                    ? formatDateTime(turnover.checkin_at, timeZone)
                    : 'None booked'}
                </dd>
              </div>
              <div>
                <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">
                  Your window
                </dt>
                <dd className="mt-0.5">
                  {formatTurnaround(turnover.checkout_at, turnover.checkin_at)}
                </dd>
              </div>
              <div>
                <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">
                  Owner&apos;s budget
                </dt>
                <dd className="mt-0.5">{formatCents(turnover.owner_budget_cents)}</dd>
              </div>
            </dl>

            {(turnover.notes || turnover.property.cleaning_notes) && (
              <div className="mt-3 space-y-1 text-sm text-slate-700">
                {turnover.property.cleaning_notes && (
                  <p className="whitespace-pre-wrap">{turnover.property.cleaning_notes}</p>
                )}
                {turnover.notes && <p className="whitespace-pre-wrap">{turnover.notes}</p>}
              </div>
            )}

            <BidBox
              turnover={turnover}
              canBid={canBid}
              blockedReason={blockedReason}
              onBid={handleBid}
            />
          </div>
        ))}
      </div>
    </div>
  )
}
