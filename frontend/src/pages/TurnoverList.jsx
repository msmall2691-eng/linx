import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import Alert from '../components/Alert.jsx'
import EmptyState from '../components/EmptyState.jsx'
import StatusBadge from '../components/StatusBadge.jsx'
import UrgencyBadge from '../components/UrgencyBadge.jsx'
import { apiFetch } from '../lib/api.js'
import { useTimeZone } from '../lib/config.jsx'
import { formatDateTime, formatTurnaround } from '../lib/datetime.js'
import { showsUrgency } from '../lib/turnover.js'

//: How many jobs a page asks for. The endpoint caps `limit` at 200; asking for
//: a screenful at a time and offering more is kinder than one enormous query,
//: and `offset` is what makes the rest reachable at all.
const PAGE = 50

export default function TurnoverList() {
  const timeZone = useTimeZone()

  const [turnovers, setTurnovers] = useState(null)
  const [properties, setProperties] = useState([])
  const [showFinished, setShowFinished] = useState(false)
  const [error, setError] = useState(null)
  const [more, setMore] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)

  useEffect(() => {
    let cancelled = false
    setTurnovers(null)
    setMore(false)
    Promise.all([
      apiFetch(`/turnovers?include_finished=${showFinished}&limit=${PAGE}`),
      apiFetch('/properties?include_archived=true'),
    ])
      .then(([loadedTurnovers, loadedProperties]) => {
        if (cancelled) return
        setTurnovers(loadedTurnovers)
        setMore(loadedTurnovers.length === PAGE)
        setProperties(loadedProperties)
      })
      .catch((err) => !cancelled && setError(err.message))
    return () => {
      cancelled = true
    }
  }, [showFinished])

  /**
   * **A job the list cannot reach is a job nobody can post, edit or cancel.**
   *
   * The endpoint has always taken `limit` and `offset`; this screen asked for
   * neither and so showed the API's default fifty with no way past them. That
   * was survivable while jobs arrived one form at a time, and stopped being so
   * the moment `create_many` could add a hundred in one go — the drafts past
   * the fiftieth existed, were charged nothing, notified nobody, and could not
   * be opened.
   */
  async function loadMore() {
    setLoadingMore(true)
    try {
      const next = await apiFetch(
        `/turnovers?include_finished=${showFinished}&limit=${PAGE}` +
          `&offset=${turnovers.length}`,
      )
      setTurnovers((current) => [...current, ...next])
      setMore(next.length === PAGE)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoadingMore(false)
    }
  }

  const propertyName = (id) =>
    properties.find((p) => p.id === id)?.nickname ?? 'Unknown property'

  return (
    <div className="mx-auto max-w-3xl px-4 py-10">
      <div className="flex items-center justify-between gap-4">
        <h1 className="text-2xl font-bold tracking-tight">Turnovers</h1>
        <div className="flex items-center gap-3">
          {/* Mounted here rather than tucked behind a property, because the
              owner who needs it most has no calendar feed and so never visits
              the panel where one would be connected. */}
          <Link
            to="/turnovers/bulk"
            className="btn-secondary"
            data-testid="bulk-link"
          >
            Add several
          </Link>
          <Link to="/turnovers/new" className="btn-primary">
            Post a turnover
          </Link>
        </div>
      </div>

      <label className="mt-4 flex items-center gap-2 text-sm text-slate-600">
        <input
          type="checkbox"
          checked={showFinished}
          onChange={(e) => setShowFinished(e.target.checked)}
          className="rounded border-slate-300 text-brand-600 focus:ring-brand-500"
        />
        Show completed and cancelled
      </label>

      <div className="mt-6 space-y-3">
        <Alert>{error}</Alert>

        {turnovers === null && !error && <p className="text-sm text-slate-500">Loading…</p>}

        {turnovers?.length === 0 && (
          <EmptyState
            title="Nothing on the schedule"
            body="Post a turnover with the checkout and the next checkin, and cleaners nearby can bid on it."
            actionLabel="Post a turnover"
            actionTo="/turnovers/new"
          />
        )}

        {turnovers?.map((turnover) => (
          <Link
            key={turnover.id}
            to={`/turnovers/${turnover.id}`}
            className="card block transition hover:border-brand-300 hover:shadow"
          >
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <h2 className="font-semibold">{propertyName(turnover.property_id)}</h2>
                <p className="mt-1 text-sm text-slate-600">
                  Checkout {formatDateTime(turnover.checkout_at, timeZone)}
                </p>
                <p className="text-sm text-slate-500">
                  {formatTurnaround(turnover.checkout_at, turnover.checkin_at)}
                </p>
              </div>
              <div className="flex shrink-0 flex-col items-end gap-2">
                {showsUrgency(turnover.status) && <UrgencyBadge urgency={turnover.urgency} />}
                <StatusBadge status={turnover.status} />
              </div>
            </div>
          </Link>
        ))}

        {more && (
          <button
            type="button"
            className="btn-secondary w-full"
            onClick={loadMore}
            disabled={loadingMore}
            data-testid="load-more"
          >
            {loadingMore ? 'Loading…' : 'Show more'}
          </button>
        )}
      </div>
    </div>
  )
}
