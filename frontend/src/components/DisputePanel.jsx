import { useEffect, useState } from 'react'

import Alert from './Alert.jsx'
import { apiFetch } from '../lib/api.js'
import { useTimeZone } from '../lib/config.jsx'
import { formatDateTime } from '../lib/datetime.js'

/**
 * Raising a dispute about a job, and reading your own.
 *
 * Used by both sides — an owner on their turnover page, a cleaner on their job
 * card — because both have the same right to complain about a job they were on
 * together.
 *
 * **It shows only your own.** A dispute the other side raised is not here, and
 * neither is a count of them: at v1 a person decides when somebody is told they
 * are being complained about, and a badge on this screen would make that
 * decision for them.
 *
 * Deliberately behind a link rather than open by default. A complaint form
 * sitting permanently under every finished job invites one; this is the version
 * you go looking for.
 */
const REASONS = [
  { value: 'quality', label: 'The clean itself' },
  { value: 'access', label: 'Getting in' },
  { value: 'damage', label: 'Damage' },
  { value: 'payment', label: 'Money' },
  { value: 'conduct', label: 'How someone behaved' },
  { value: 'other', label: 'Something else' },
]

const STATUS_WORDS = {
  open: 'Waiting for someone to pick it up',
  acknowledged: 'Someone is looking at it',
  resolved: 'Resolved',
}

function DisputeCard({ dispute, timeZone }) {
  return (
    <div className="rounded-lg bg-slate-50 p-3" data-testid="dispute">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-xs font-medium uppercase tracking-wide text-slate-500">
          {REASONS.find((r) => r.value === dispute.reason)?.label ?? dispute.reason}
        </span>
        <span className="text-xs text-slate-500" data-testid="dispute-status">
          {STATUS_WORDS[dispute.status] ?? dispute.status}
        </span>
      </div>
      <p className="mt-2 whitespace-pre-wrap text-sm text-slate-700">{dispute.description}</p>
      <p className="mt-2 text-xs text-slate-400">
        Raised {formatDateTime(dispute.created_at, timeZone)}
      </p>

      {dispute.resolution_notes && (
        <div className="mt-3 rounded border border-slate-200 bg-white p-3">
          <p className="text-xs font-medium uppercase tracking-wide text-slate-500">
            What was decided
          </p>
          {/* The admin's note, verbatim. A decision somebody has to live with
              should reach them in the words of the person who made it. */}
          <p className="mt-1 whitespace-pre-wrap text-sm text-slate-700" data-testid="dispute-resolution">
            {dispute.resolution_notes}
          </p>
        </div>
      )}
    </div>
  )
}

export default function DisputePanel({ turnoverId }) {
  const timeZone = useTimeZone()

  const [state, setState] = useState(null)
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState('quality')
  const [awardId, setAwardId] = useState('')
  const [description, setDescription] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let cancelled = false
    apiFetch(`/turnovers/${turnoverId}/disputes`)
      .then((data) => !cancelled && setState(data))
      .catch((err) => !cancelled && setError(err.message))
    return () => {
      cancelled = true
    }
  }, [turnoverId])

  async function submit(event) {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      // The action answers with the same shape the GET did, so replacing state
      // with it keeps everything the screen was showing.
      setState(
        await apiFetch(`/turnovers/${turnoverId}/disputes`, {
          method: 'POST',
          // Sent only when there is a choice to send. The server refuses to
          // guess between several bookings and accepts the one obvious answer
          // without being told, so an empty string must not become a value.
          body: awardId
            ? { reason, description, award_id: awardId }
            : { reason, description },
        }),
      )
      setDescription('')
      setAwardId('')
      setOpen(false)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  // **A failure to load is not the same as nothing to show.** Returning null
  // on `!state` alone made a network or server error look exactly like a
  // turnover with no dispute feature: the error was stored and then rendered
  // nowhere, so somebody whose complaint failed to load was told nothing at
  // all, on the one screen in the product for telling somebody something went
  // wrong.
  if (!state && error) {
    return (
      <div className="mt-6 card" data-testid="dispute-panel">
        <h2 className="font-semibold">Something go wrong?</h2>
        <div className="mt-3">
          <Alert>{error}</Alert>
        </div>
      </div>
    )
  }
  if (!state) return null
  // Nothing raised and nothing raisable — no reason to take up the screen.
  if (!state.can_raise && state.mine.length === 0) return null

  return (
    <div className="mt-6 card" data-testid="dispute-panel">
      <h2 className="font-semibold">Something go wrong?</h2>

      {state.mine.length > 0 && (
        <div className="mt-3 space-y-3">
          {state.mine.map((dispute) => (
            <DisputeCard key={dispute.id} dispute={dispute} timeZone={timeZone} />
          ))}
        </div>
      )}

      {state.can_raise && !open && (
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="btn-secondary mt-3"
          data-testid="open-dispute-form"
        >
          Raise a dispute
        </button>
      )}

      {state.can_raise && open && (
        <form onSubmit={submit} className="mt-3 space-y-3">
          <p className="text-sm text-slate-600">
            A person reads every one of these. Nothing is decided automatically, and
            nobody is charged or refunded because a dispute was opened.
          </p>

          {/* **Only when there is a real choice.** A turnover that was
              cancelled and re-awarded has more than one booking in its
              history, and the server refuses to guess which one a complaint is
              about rather than filing it against whoever holds the job today.
              One booking needs no question asked. */}
          {state.bookings.length > 1 && (
            <div>
              <label htmlFor="dispute-award" className="field-label">
                Which booking?
              </label>
              <select
                id="dispute-award"
                value={awardId}
                onChange={(e) => setAwardId(e.target.value)}
                className="field-input"
                data-testid="dispute-award"
              >
                <option value="">Choose the booking this is about…</option>
                {state.bookings.map((booking) => (
                  <option key={booking.award_id} value={booking.award_id}>
                    {booking.cleaner_name} — booked{' '}
                    {formatDateTime(booking.awarded_at, timeZone)}
                    {booking.was_no_show
                      ? ' (recorded as a no-show)'
                      : booking.cancelled_at
                        ? ' (cancelled)'
                        : ''}
                  </option>
                ))}
              </select>
            </div>
          )}

          <div>
            <label htmlFor="dispute-reason" className="field-label">
              What kind of problem?
            </label>
            <select
              id="dispute-reason"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              className="field-input"
              data-testid="dispute-reason"
            >
              {REASONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label htmlFor="dispute-description" className="field-label">
              What happened?
            </label>
            <textarea
              id="dispute-description"
              rows={4}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="As much detail as you can — this is what a person reads."
              className="field-input"
              data-testid="dispute-description"
            />
          </div>

          <div className="flex gap-2">
            <button
              type="submit"
              className="btn-primary"
              disabled={
                busy ||
                description.trim().length === 0 ||
                // The server would refuse this anyway; the button saying so
                // first is the difference between a form that waits and a form
                // that rejects what it accepted.
                (state.bookings.length > 1 && !awardId)
              }
              data-testid="submit-dispute"
            >
              {busy ? 'Sending…' : 'Send it'}
            </button>
            <button type="button" className="btn-secondary" onClick={() => setOpen(false)}>
              Cancel
            </button>
          </div>
        </form>
      )}

      {!state.can_raise && state.blocker && (
        <p className="mt-3 text-sm text-slate-600" data-testid="dispute-blocker">
          {state.blocker}
        </p>
      )}

      {error && (
        <div className="mt-4">
          <Alert>{error}</Alert>
        </div>
      )}
    </div>
  )
}
