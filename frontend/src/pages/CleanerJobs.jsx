import { useEffect, useState } from 'react'

import Alert from '../components/Alert.jsx'
import EmptyState from '../components/EmptyState.jsx'
import { ScopeBadge } from '../components/JobScope.jsx'
import ReviewPanel from '../components/ReviewPanel.jsx'
import StatusBadge from '../components/StatusBadge.jsx'
import UrgencyBadge from '../components/UrgencyBadge.jsx'
import { apiFetch } from '../lib/api.js'
import { useTimeZone } from '../lib/config.jsx'
import { formatCents, formatDateTime, formatTurnaround } from '../lib/datetime.js'
import { showsUrgency } from '../lib/turnover.js'

/**
 * The jobs this cleaner has actually been hired for.
 *
 * This is the other side of the bench board's privacy boundary: the street
 * address and the access notes are here, because somebody has to open the door.
 * They arrive from the API only while the booking is live — a cancelled job
 * comes back with the access notes empty, which is why this screen renders
 * whatever the field holds rather than deciding for itself.
 */
function Job({ job, timeZone, onCancel, onStart, onComplete, busy }) {
  const [confirming, setConfirming] = useState(false)
  const [reason, setReason] = useState('')
  const property = job.property
  const cancelled = Boolean(job.cancelled_at)
  const started = Boolean(job.started_at)
  const done = Boolean(job.completed_at)

  return (
    <li className="card" data-testid="job">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="font-semibold" data-testid="job-nickname">
            {property.nickname}
          </h2>
          <p className="text-sm text-slate-600" data-testid="job-address">
            {property.address_line1}
            {property.address_line2 ? `, ${property.address_line2}` : ''}, {property.city},{' '}
            {property.state} {property.postal_code}
          </p>
        </div>
        <div className="flex gap-2">
          {showsUrgency(job.status) && <UrgencyBadge urgency={job.urgency} />}
          <ScopeBadge serviceType={job.service_type} />
          <StatusBadge status={job.status} />
        </div>
      </div>

      <dl className="mt-4 grid gap-4 sm:grid-cols-2">
        <div>
          <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">
            Guest checks out
          </dt>
          <dd className="mt-1 text-sm font-medium">
            {formatDateTime(job.checkout_at, timeZone)}
          </dd>
        </div>
        <div>
          <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">
            Next guest checks in
          </dt>
          <dd className="mt-1 text-sm font-medium">
            {job.checkin_at ? (
              formatDateTime(job.checkin_at, timeZone)
            ) : (
              <span className="text-slate-400">None booked yet</span>
            )}
          </dd>
        </div>
        <div>
          <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">
            Your window
          </dt>
          <dd className="mt-1 text-sm">{formatTurnaround(job.checkout_at, job.checkin_at)}</dd>
        </div>
        <div>
          <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">
            Agreed price
          </dt>
          <dd className="mt-1 text-sm font-medium" data-testid="job-price">
            {formatCents(job.agreed_price_cents)}
          </dd>
        </div>

        {property.access_notes && (
          <div className="sm:col-span-2">
            <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">
              Getting in
            </dt>
            <dd
              className="mt-1 whitespace-pre-wrap rounded-lg bg-slate-50 p-3 text-sm"
              data-testid="job-access-notes"
            >
              {property.access_notes}
            </dd>
          </div>
        )}

        {property.cleaning_notes && (
          <div className="sm:col-span-2">
            <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">
              Cleaning notes
            </dt>
            <dd className="mt-1 whitespace-pre-wrap text-sm">{property.cleaning_notes}</dd>
          </div>
        )}

        {job.notes && (
          <div className="sm:col-span-2">
            <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">
              From the owner
            </dt>
            <dd className="mt-1 whitespace-pre-wrap text-sm">{job.notes}</dd>
          </div>
        )}

        {cancelled && (
          <div className="sm:col-span-2">
            <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">
              {job.was_no_show ? 'Reported as a no-show' : 'Cancelled'}
            </dt>
            <dd className="mt-1 text-sm">
              {formatDateTime(job.cancelled_at, timeZone)}
              {job.cancellation_reason ? ` — ${job.cancellation_reason}` : ''}
            </dd>
          </div>
        )}
      </dl>

      {done && (
        <>
          <div className="mt-6 rounded-lg bg-emerald-50 p-3 text-sm" data-testid="job-done">
            <p className="font-medium text-emerald-900">You marked this done.</p>
            <p className="mt-1 text-emerald-800">
              The owner has been asked to pay. Your share lands in your Stripe account
              once they do.
            </p>
          </div>
          <ReviewPanel turnoverId={job.turnover_id} side="cleaner" />
        </>
      )}

      {!cancelled && !done && (
        <div className="mt-6 flex flex-wrap gap-2 border-t border-slate-200 pt-4">
          {!started && (
            <button
              type="button"
              onClick={() => onStart(job.turnover_id)}
              disabled={busy}
              className="btn-secondary"
              data-testid="start-job"
            >
              I&rsquo;m on site
            </button>
          )}
          <button
            type="button"
            onClick={() => onComplete(job.turnover_id)}
            disabled={busy}
            className="btn-primary"
            data-testid="complete-job"
          >
            {busy ? 'Saving…' : 'Mark this job done'}
          </button>
        </div>
      )}

      {!cancelled && !done && (
        <div className="mt-4 border-t border-slate-200 pt-4">
          {confirming ? (
            <div className="space-y-3">
              <label htmlFor={`why-${job.turnover_id}`} className="field-label">
                Why can&rsquo;t you make it?{' '}
                <span className="font-normal text-slate-500">— the owner is told</span>
              </label>
              <textarea
                id={`why-${job.turnover_id}`}
                rows={2}
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                placeholder="My van is off the road."
                className="field-input"
              />
              <p className="text-xs text-slate-500">
                The job goes straight back to the bench so the owner can find someone else.
                Let them know as early as you can — a cancellation close to checkout is
                flagged to them and to an admin.
              </p>
              <div className="flex gap-2">
                <button
                  type="button"
                  disabled={busy || !reason.trim()}
                  onClick={() => onCancel(job.turnover_id, reason)}
                  className="btn-primary bg-red-600 hover:bg-red-700 disabled:opacity-50"
                  data-testid="confirm-cancel-job"
                >
                  {busy ? 'Cancelling…' : 'Cancel this job'}
                </button>
                <button
                  type="button"
                  onClick={() => setConfirming(false)}
                  className="btn-secondary"
                >
                  Keep it
                </button>
              </div>
            </div>
          ) : (
            <button
              type="button"
              onClick={() => setConfirming(true)}
              className="btn-secondary text-red-700"
              data-testid="cancel-job"
            >
              I can&rsquo;t make this one
            </button>
          )}
        </div>
      )}
    </li>
  )
}

export default function CleanerJobs() {
  const timeZone = useTimeZone()
  const [jobs, setJobs] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  async function load() {
    try {
      setJobs(await apiFetch('/board/jobs'))
    } catch (err) {
      setError(err.message)
    }
  }

  useEffect(() => {
    let cancelled = false
    apiFetch('/board/jobs')
      .then((loaded) => !cancelled && setJobs(loaded))
      .catch((err) => !cancelled && setError(err.message))
    return () => {
      cancelled = true
    }
  }, [])

  async function act(turnoverId, action) {
    setError(null)
    setBusy(true)
    try {
      const updated = await apiFetch(`/board/jobs/${turnoverId}/${action}`, {
        method: 'POST',
      })
      // The action answers with the whole job — the same shape the list was
      // built from — so it can be spliced in rather than re-fetched. An action
      // that returned less than the GET would blank the card it replaced.
      setJobs((prev) =>
        prev.map((job) => (job.turnover_id === turnoverId ? updated : job)),
      )
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function cancelJob(turnoverId, reason) {
    setError(null)
    setBusy(true)
    try {
      await apiFetch(`/board/jobs/${turnoverId}/cancel`, {
        method: 'POST',
        body: { reason },
      })
      // Re-read rather than splicing the answer in: the job is no longer a
      // live booking, so it drops off this list entirely.
      await load()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mx-auto max-w-3xl px-4 py-10">
      <h1 className="text-2xl font-bold tracking-tight">Your jobs</h1>
      <p className="mt-1 text-sm text-slate-600">
        Work you have been hired for, soonest first.
      </p>

      <div className="mt-4">
        <Alert>{error}</Alert>
      </div>

      {jobs === null ? (
        <p className="mt-6 text-sm text-slate-500">Loading…</p>
      ) : jobs.length === 0 ? (
        <div className="mt-6">
          <EmptyState
            title="No jobs booked yet"
            body="When an owner accepts one of your bids it shows up here, with the address and how to get in."
            actionLabel="See open turnovers"
            actionTo="/board"
          />
        </div>
      ) : (
        <ul className="mt-6 space-y-4">
          {jobs.map((job) => (
            <Job
              key={job.award_id}
              job={job}
              timeZone={timeZone}
              onCancel={cancelJob}
              onStart={(id) => act(id, 'start')}
              onComplete={(id) => act(id, 'complete')}
              busy={busy}
            />
          ))}
        </ul>
      )}
    </div>
  )
}
