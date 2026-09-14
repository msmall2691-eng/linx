import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import Alert from '../components/Alert.jsx'
import EmptyState from '../components/EmptyState.jsx'
import UrgencyBadge from '../components/UrgencyBadge.jsx'
import { apiFetch } from '../lib/api.js'
import { useTimeZone } from '../lib/config.jsx'
import { formatCents, formatDateTime } from '../lib/datetime.js'

/**
 * The admin console.
 *
 * Three things that existed as data nobody could see, given a screen: the
 * dispute inbox, the unclaimed alarm, and the ledger. The vetting queue keeps
 * its own page and is linked from here rather than rebuilt — it works, it is
 * tested, and the trust gate is the last thing worth destabilising.
 *
 * **The summary leads, and the drift number is the one that matters.**
 * `collected − paid out − fee` has to be zero. Anything else means money moved
 * that the codebase cannot account for, which is the one number here worth
 * waking somebody up for.
 */
const REASON_LABELS = {
  quality: 'The clean itself',
  access: 'Getting in',
  damage: 'Damage',
  payment: 'Money',
  conduct: 'Conduct',
  other: 'Other',
}

function Stat({ label, value, tone = 'normal', to }) {
  const alarming = tone === 'alarm' && value > 0
  const body = (
    <div
      className={`card ${alarming ? 'border-rose-300 bg-rose-50' : ''}`}
      data-testid={`stat-${label.toLowerCase().replace(/\s+/g, '-')}`}
    >
      <p className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</p>
      <p
        className={`mt-1 text-2xl font-semibold ${alarming ? 'text-rose-700' : 'text-slate-900'}`}
      >
        {value}
      </p>
    </div>
  )
  return to ? (
    <Link to={to} className="block transition hover:opacity-80">
      {body}
    </Link>
  ) : (
    body
  )
}

function DisputeRow({ dispute, onChange, timeZone }) {
  const [notes, setNotes] = useState('')
  const [resolving, setResolving] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function act(path, body) {
    setBusy(true)
    setError('')
    try {
      onChange(await apiFetch(path, { method: 'POST', body }))
      setResolving(false)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const resolved = dispute.status === 'resolved'

  return (
    <li
      className={`rounded-lg border p-4 ${
        resolved ? 'border-slate-200 bg-slate-50' : 'border-amber-300 bg-white'
      }`}
      data-testid="admin-dispute"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="font-medium">
            {REASON_LABELS[dispute.reason] ?? dispute.reason}
            <span className="ml-2 text-sm font-normal text-slate-500">
              raised by the {dispute.raised_by_role}
            </span>
          </p>
          <p className="mt-1 text-sm text-slate-600">
            <Link to={`/turnovers/${dispute.turnover_id}`} className="underline">
              {dispute.property_nickname}
            </Link>{' '}
            — {dispute.property_city}, {dispute.property_state} · checkout{' '}
            {formatDateTime(dispute.checkout_at, timeZone)}
          </p>
        </div>
        <span className="shrink-0 text-xs text-slate-500" data-testid="admin-dispute-status">
          {dispute.status}
          {dispute.acknowledged_at && !resolved && ' · picked up'}
        </span>
      </div>

      <p className="mt-3 whitespace-pre-wrap text-sm text-slate-800">{dispute.description}</p>

      {/* Both sides, with contact details: settling a disagreement between two
          people you cannot reach is not possible. This is the one screen in the
          product where the owner's identity is shown to anybody but themselves. */}
      <dl className="mt-3 grid gap-2 text-xs text-slate-600 sm:grid-cols-2">
        <div>
          <dt className="font-medium uppercase tracking-wide text-slate-500">Owner</dt>
          <dd>
            {dispute.owner.full_name} · {dispute.owner.email}
            {dispute.owner.phone && ` · ${dispute.owner.phone}`}
          </dd>
        </div>
        <div>
          <dt className="font-medium uppercase tracking-wide text-slate-500">Cleaner</dt>
          <dd>
            {dispute.cleaner.full_name} · {dispute.cleaner.email}
            {dispute.cleaner.phone && ` · ${dispute.cleaner.phone}`}
          </dd>
        </div>
      </dl>

      {(dispute.award_cancelled_at || dispute.award_was_no_show) && (
        <p className="mt-2 text-xs text-slate-500">
          {dispute.award_was_no_show
            ? 'The cleaner was recorded as a no-show on this job.'
            : 'The booking on this job was cancelled.'}
        </p>
      )}

      {dispute.resolution_notes && (
        <div className="mt-3 rounded border border-slate-200 bg-white p-3">
          <p className="text-xs font-medium uppercase tracking-wide text-slate-500">Resolved</p>
          <p className="mt-1 whitespace-pre-wrap text-sm text-slate-700">
            {dispute.resolution_notes}
          </p>
        </div>
      )}

      {!resolved && (
        <div className="mt-4 flex flex-wrap gap-2">
          {!dispute.acknowledged_at && (
            <button
              type="button"
              className="btn-secondary"
              disabled={busy}
              onClick={() => act(`/admin/disputes/${dispute.id}/acknowledge`)}
              data-testid="acknowledge-dispute"
            >
              I&rsquo;ve got this
            </button>
          )}
          {!resolving && (
            <button
              type="button"
              className="btn-primary"
              disabled={busy}
              onClick={() => setResolving(true)}
              data-testid="open-resolve-form"
            >
              Resolve
            </button>
          )}
        </div>
      )}

      {resolving && (
        <div className="mt-3 space-y-2">
          <label htmlFor={`notes-${dispute.id}`} className="field-label">
            What was decided, and why
          </label>
          <textarea
            id={`notes-${dispute.id}`}
            rows={3}
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            className="field-input"
            placeholder="Both sides are sent this, word for word."
            data-testid="resolution-notes"
          />
          {/* Resolving refunds nothing. "The dispute is closed" and "the owner
              got their money back" are different sentences and must not be one
              click — the refund lives on the ledger, with its own reason. */}
          <p className="text-xs text-slate-500">
            This sends both sides your note. It does not refund anything — if money
            should move, do that on the ledger below.
          </p>
          <div className="flex gap-2">
            <button
              type="button"
              className="btn-primary"
              disabled={busy || notes.trim().length === 0}
              onClick={() => act(`/admin/disputes/${dispute.id}/resolve`, { notes })}
              data-testid="submit-resolution"
            >
              {busy ? 'Sending…' : 'Resolve and tell both sides'}
            </button>
            <button type="button" className="btn-secondary" onClick={() => setResolving(false)}>
              Cancel
            </button>
          </div>
        </div>
      )}

      {error && (
        <div className="mt-3">
          <Alert>{error}</Alert>
        </div>
      )}
    </li>
  )
}

function LedgerTable({ ledger, onRefund, timeZone }) {
  const [refunding, setRefunding] = useState(null)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function refund(turnoverId) {
    setBusy(true)
    setError('')
    try {
      await apiFetch(`/turnovers/${turnoverId}/refund`, {
        method: 'POST',
        body: { reason },
      })
      setRefunding(null)
      setReason('')
      await onRefund()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  if (ledger.rows.length === 0) {
    return (
      <EmptyState
        title="No money has moved yet"
        body="Payments appear here the moment a job is paid for, with what was collected, what reached the cleaner, and whether the two still agree."
      />
    )
  }

  return (
    <>
      <div className="card">
        <dl className="grid gap-4 sm:grid-cols-4">
          <div>
            <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">
              Collected
            </dt>
            <dd className="mt-1 font-semibold">
              {formatCents(ledger.total_collected_cents)}
            </dd>
          </div>
          <div>
            <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">
              Paid out
            </dt>
            <dd className="mt-1 font-semibold">{formatCents(ledger.total_paid_out_cents)}</dd>
          </div>
          <div>
            <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">
              Platform fee
            </dt>
            <dd className="mt-1 font-semibold">
              {formatCents(ledger.total_platform_fee_cents)}
            </dd>
          </div>
          <div>
            <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">Drift</dt>
            <dd
              className={`mt-1 font-semibold ${
                ledger.total_drift_cents === 0 ? 'text-emerald-700' : 'text-rose-700'
              }`}
              data-testid="total-drift"
            >
              {formatCents(ledger.total_drift_cents)}
            </dd>
          </div>
        </dl>
        <p className="mt-3 text-xs text-slate-500">
          Collected minus paid out minus the fee is drift, and drift is zero or something
          moved that this system cannot account for.
        </p>
      </div>

      <div className="mt-4 overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead className="text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th className="py-2 pr-3">Job</th>
              <th className="py-2 pr-3">Status</th>
              <th className="py-2 pr-3 text-right">Collected</th>
              <th className="py-2 pr-3 text-right">Paid out</th>
              <th className="py-2 pr-3 text-right">Fee</th>
              <th className="py-2 pr-3 text-right">Drift</th>
              <th className="py-2" />
            </tr>
          </thead>
          <tbody>
            {ledger.rows.map((row) => (
              <tr key={row.turnover_id} className="border-t border-slate-200" data-testid="ledger-row">
                <td className="py-2 pr-3">
                  <Link to={`/turnovers/${row.turnover_id}`} className="underline">
                    {row.property_nickname}
                  </Link>
                  <span className="block text-xs text-slate-500">
                    {formatDateTime(row.checkout_at, timeZone)} · {row.cleaner_name}
                  </span>
                </td>
                <td className="py-2 pr-3">{row.status}</td>
                <td className="py-2 pr-3 text-right">{formatCents(row.collected_cents)}</td>
                <td className="py-2 pr-3 text-right">{formatCents(row.paid_out_cents)}</td>
                <td className="py-2 pr-3 text-right">{formatCents(row.platform_fee_cents)}</td>
                <td
                  className={`py-2 pr-3 text-right ${
                    row.drift_cents === 0 ? 'text-slate-500' : 'font-semibold text-rose-700'
                  }`}
                  data-testid="row-drift"
                >
                  {formatCents(row.drift_cents)}
                </td>
                <td className="py-2 text-right">
                  {row.status === 'succeeded' && (
                    <button
                      type="button"
                      className="btn-secondary"
                      onClick={() => setRefunding(row.turnover_id)}
                      data-testid="open-refund"
                    >
                      Refund
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {refunding && (
        <div className="mt-4 card border-rose-200">
          <h3 className="font-semibold">Refund this job in full</h3>
          {/* Full refunds only at v1: a partial one has to decide how to split
              the shortfall, and that is a policy question with somebody's
              income on the other end. */}
          <p className="mt-1 text-sm text-slate-600">
            The owner is made whole, the platform fee comes back, and the cleaner&rsquo;s
            transfer is reversed. They have already been told they earned it, so say why.
          </p>
          <textarea
            rows={3}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            className="field-input mt-3"
            placeholder="The reason this is being refunded."
            aria-label="Refund reason"
            data-testid="refund-reason"
          />
          <div className="mt-3 flex gap-2">
            <button
              type="button"
              className="btn-primary"
              disabled={busy || reason.trim().length === 0}
              onClick={() => refund(refunding)}
              data-testid="submit-refund"
            >
              {busy ? 'Refunding…' : 'Refund in full'}
            </button>
            <button
              type="button"
              className="btn-secondary"
              onClick={() => {
                setRefunding(null)
                setReason('')
              }}
            >
              Cancel
            </button>
          </div>
          {error && (
            <div className="mt-3">
              <Alert>{error}</Alert>
            </div>
          )}
        </div>
      )}
    </>
  )
}

export default function AdminConsole() {
  const timeZone = useTimeZone()

  const [summary, setSummary] = useState(null)
  const [disputes, setDisputes] = useState(null)
  const [unclaimed, setUnclaimed] = useState(null)
  const [ledger, setLedger] = useState(null)
  const [showResolved, setShowResolved] = useState(false)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    try {
      const [s, d, u, l] = await Promise.all([
        apiFetch('/admin/summary'),
        apiFetch(`/admin/disputes?include_resolved=${showResolved}`),
        apiFetch('/admin/unclaimed'),
        apiFetch('/admin/ledger'),
      ])
      setSummary(s)
      setDisputes(d)
      setUnclaimed(u)
      setLedger(l)
    } catch (err) {
      setError(err.message)
    }
  }, [showResolved])

  useEffect(() => {
    load()
  }, [load])

  function replaceDispute(updated) {
    setDisputes((current) =>
      (current ?? []).map((d) => (d.id === updated.id ? updated : d)),
    )
    // The counts move when a dispute does, and a summary that disagrees with
    // the list under it is worse than no summary.
    apiFetch('/admin/summary').then(setSummary).catch(() => {})
  }

  return (
    <div className="mx-auto max-w-5xl px-4 py-10">
      <h1 className="text-2xl font-bold tracking-tight">Console</h1>
      <p className="mt-1 text-sm text-slate-600">
        What needs a person. Nothing here is decided automatically.
      </p>

      <Alert>{error}</Alert>

      {summary && (
        <div className="mt-6 grid gap-4 sm:grid-cols-4">
          <Stat label="Open disputes" value={summary.open_disputes} />
          <Stat label="Vetting pending" value={summary.vetting_pending} to="/admin/vetting" />
          <Stat label="Unclaimed" value={summary.unclaimed_turnovers} />
          <Stat label="Payments with drift" value={summary.payments_with_drift} tone="alarm" />
        </div>
      )}

      <section className="mt-10">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 className="text-lg font-semibold">Disputes</h2>
          <label className="flex items-center gap-2 text-sm text-slate-600">
            <input
              type="checkbox"
              checked={showResolved}
              onChange={(e) => setShowResolved(e.target.checked)}
              className="rounded border-slate-300 text-brand-600 focus:ring-brand-500"
            />
            Show resolved
          </label>
        </div>

        {disputes?.length === 0 ? (
          <div className="mt-4">
            <EmptyState
              title="Nothing to settle"
              body="Disputes raised by an owner or a cleaner land here, oldest first. A person reads every one."
            />
          </div>
        ) : (
          <ul className="mt-4 space-y-3" data-testid="dispute-inbox">
            {disputes?.map((dispute) => (
              <DisputeRow
                key={dispute.id}
                dispute={dispute}
                onChange={replaceDispute}
                timeZone={timeZone}
              />
            ))}
          </ul>
        )}
      </section>

      <section className="mt-10">
        <h2 className="text-lg font-semibold">Unclaimed</h2>
        <p className="mt-1 text-sm text-slate-600">
          Open jobs with checkout close and nobody booked. The same cutoff the alert email
          uses — one definition, so the screen and the email cannot disagree.
        </p>

        {unclaimed?.length === 0 ? (
          <div className="mt-4">
            <EmptyState title="Everything coming up is staffed" />
          </div>
        ) : (
          <ul className="mt-4 space-y-3" data-testid="unclaimed-list">
            {unclaimed?.map((job) => (
              <li
                key={job.turnover_id}
                className="card flex flex-wrap items-start justify-between gap-3"
                data-testid="unclaimed-row"
              >
                <div>
                  <Link to={`/turnovers/${job.turnover_id}`} className="font-medium underline">
                    {job.property_nickname}
                  </Link>
                  <p className="mt-1 text-sm text-slate-600">
                    {job.property_city}, {job.property_state} · checkout{' '}
                    {formatDateTime(job.checkout_at, timeZone)}
                  </p>
                  <p className="text-sm text-slate-500">
                    {/* Zero bids and "four bids, none accepted" are different
                        problems: a supply gap, or an owner who has not chosen. */}
                    {job.bid_count === 0
                      ? 'No bids yet'
                      : `${job.bid_count} bid${job.bid_count === 1 ? '' : 's'}, none accepted`}
                    {' · '}
                    {job.owner_name} ({job.owner_email})
                  </p>
                </div>
                <UrgencyBadge urgency={job.urgency} />
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="mt-10">
        <h2 className="text-lg font-semibold">Ledger</h2>
        <div className="mt-4">
          {ledger && <LedgerTable ledger={ledger} onRefund={load} timeZone={timeZone} />}
        </div>
      </section>

      <p className="mt-10 text-sm text-slate-500">
        Vetting has its own screen:{' '}
        <Link to="/admin/vetting" className="underline">
          the review queue
        </Link>
        .
      </p>
    </div>
  )
}
