import { useEffect, useState } from 'react'

import Alert from '../components/Alert.jsx'
import EmptyState from '../components/EmptyState.jsx'
import { apiFetch, getToken } from '../lib/api.js'
import { useTimeZone } from '../lib/config.jsx'
import { formatDateTime } from '../lib/datetime.js'

const TYPE_LABELS = { id: 'Photo ID', insurance: 'Insurance', reference: 'Reference' }

/**
 * The manual review queue.
 *
 * Deliberately has no control for `can_take_jobs`. Postgres computes it from
 * the two vetting statuses below, and there is no endpoint to override it —
 * an override is how someone ends up able to bid without a finished check.
 */
function Cleaner({ entry, onChange, timeZone }) {
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  async function act(path, body) {
    setError(null)
    setBusy(true)
    try {
      onChange(await apiFetch(path, { method: 'POST', body }))
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  function openDocument(documentId) {
    // Documents are never on a public path; this fetches with the admin's
    // token and hands the browser a blob.
    fetch(`/api/admin/documents/${documentId}/file`, {
      headers: { Authorization: `Bearer ${getToken()}` },
    })
      .then((response) => {
        if (!response.ok) throw new Error(`Could not open the document (${response.status})`)
        return response.blob()
      })
      .then((blob) => window.open(URL.createObjectURL(blob), '_blank', 'noopener'))
      .catch((err) => setError(err.message))
  }

  const statusRow = (label, value) => (
    <div>
      <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</dt>
      <dd className="mt-0.5 text-sm">{String(value).replace(/_/g, ' ')}</dd>
    </div>
  )

  return (
    <div className="card">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="font-semibold">{entry.full_name}</h2>
          <p className="mt-1 text-sm text-slate-600">
            {entry.email}
            {entry.phone ? ` · ${entry.phone}` : ''}
          </p>
          <p className="text-xs text-slate-500">
            Joined {formatDateTime(entry.created_at, timeZone)}
          </p>
        </div>
        <span
          className={`rounded-full px-2.5 py-0.5 text-xs font-semibold ${
            entry.can_take_jobs
              ? 'bg-emerald-100 text-emerald-800'
              : 'bg-amber-100 text-amber-800'
          }`}
        >
          {entry.can_take_jobs ? 'Cleared' : 'Not cleared'}
        </span>
      </div>

      <div className="mt-4">
        <Alert>{error}</Alert>
      </div>

      {entry.bio && <p className="mt-3 whitespace-pre-wrap text-sm text-slate-700">{entry.bio}</p>}

      <dl className="mt-4 grid gap-3 sm:grid-cols-3">
        {statusRow('ID verification', entry.id_verification_status)}
        {statusRow('Background check', entry.background_check_status)}
        {statusRow('Insurance on file', entry.has_insurance_on_file ? 'yes' : 'no')}
      </dl>

      <h3 className="mt-5 text-sm font-semibold">Documents</h3>
      {entry.documents.length === 0 ? (
        <p className="mt-1 text-sm text-slate-500">Nothing uploaded yet.</p>
      ) : (
        <ul className="mt-2 space-y-2">
          {entry.documents.map((doc) => (
            <li
              key={doc.id}
              className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-slate-50 px-3 py-2 text-sm"
            >
              <span>
                {TYPE_LABELS[doc.type] ?? doc.type}
                <span className="ml-2 text-xs text-slate-500">{doc.status}</span>
              </span>
              <span className="flex gap-2">
                <button
                  type="button"
                  onClick={() => openDocument(doc.id)}
                  className="text-xs font-medium text-brand-600 hover:underline"
                >
                  View
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    act(`/admin/documents/${doc.id}/review`, { status: 'approved' })
                  }
                  className="text-xs font-medium text-emerald-700 hover:underline"
                >
                  Approve
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    act(`/admin/documents/${doc.id}/review`, { status: 'rejected' })
                  }
                  className="text-xs font-medium text-red-700 hover:underline"
                >
                  Reject
                </button>
              </span>
            </li>
          ))}
        </ul>
      )}

      <div className="mt-5 space-y-3 border-t border-slate-200 pt-4">
        <div>
          <p className="text-sm font-medium">Identity decision</p>
          <p className="text-xs text-slate-500">
            Approve only once you have seen a photo ID <em>and</em> a reference.
          </p>
          <div className="mt-2 flex gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                act(`/admin/cleaners/${entry.profile_id}/id-verification`, {
                  status: 'approved',
                })
              }
              className="btn-secondary text-emerald-700"
            >
              Approve ID
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                act(`/admin/cleaners/${entry.profile_id}/id-verification`, {
                  status: 'rejected',
                })
              }
              className="btn-secondary text-red-700"
            >
              Reject ID
            </button>
          </div>
        </div>

        <div>
          <p className="text-sm font-medium">Background check</p>
          <p className="text-xs text-slate-500">
            If the provider returned &ldquo;consider&rdquo;, read the report and follow the
            adverse-action process before rejecting.
          </p>
          <div className="mt-2 flex flex-wrap gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                act(`/admin/cleaners/${entry.profile_id}/background-check`, {
                  status: 'approved',
                })
              }
              className="btn-secondary text-emerald-700"
            >
              Mark clear
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                act(`/admin/cleaners/${entry.profile_id}/background-check`, {
                  status: 'rejected',
                })
              }
              className="btn-secondary text-red-700"
            >
              Mark not clear
            </button>
            {entry.background_check_provider_ref && (
              <button
                type="button"
                disabled={busy}
                onClick={() =>
                  act(`/admin/cleaners/${entry.profile_id}/background-check/refresh`)
                }
                className="btn-secondary"
              >
                Refresh from provider
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

export default function AdminVetting() {
  const timeZone = useTimeZone()
  const [queue, setQueue] = useState(null)
  const [showCleared, setShowCleared] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    setQueue(null)
    apiFetch(`/admin/vetting-queue?include_cleared=${showCleared}`)
      .then(setQueue)
      .catch((err) => setError(err.message))
  }, [showCleared])

  function replaceEntry(updated) {
    setQueue((previous) =>
      (previous ?? []).map((entry) =>
        entry.profile_id === updated.profile_id ? updated : entry,
      ),
    )
  }

  return (
    <div className="mx-auto max-w-3xl px-4 py-10">
      <h1 className="text-2xl font-bold tracking-tight">Vetting queue</h1>
      <p className="mt-1 text-sm text-slate-600">
        Oldest first. Every cleaner needs a reviewed photo ID, a reference, and a
        cleared background check before they can bid.
      </p>

      <label className="mt-4 flex items-center gap-2 text-sm text-slate-600">
        <input
          type="checkbox"
          checked={showCleared}
          onChange={(event) => setShowCleared(event.target.checked)}
          className="rounded border-slate-300 text-brand-600 focus:ring-brand-500"
        />
        Show cleaners already cleared
      </label>

      <div className="mt-6 space-y-4">
        <Alert>{error}</Alert>

        {queue === null && !error && <p className="text-sm text-slate-500">Loading…</p>}

        {queue?.length === 0 && (
          <EmptyState
            title="Nobody waiting"
            body="New cleaners appear here as soon as they upload an ID or start a background check."
          />
        )}

        {queue?.map((entry) => (
          <Cleaner
            key={entry.profile_id}
            entry={entry}
            onChange={replaceEntry}
            timeZone={timeZone}
          />
        ))}
      </div>
    </div>
  )
}
