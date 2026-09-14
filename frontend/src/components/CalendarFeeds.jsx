import { useEffect, useState } from 'react'

import Alert from './Alert.jsx'
import { apiFetch } from '../lib/api.js'
import { useTimeZone } from '../lib/config.jsx'
import { formatDateTime } from '../lib/datetime.js'

/**
 * Booking calendars connected to a property.
 *
 * **The feed proposes; the owner decides.** A booking becomes a *draft*
 * turnover they confirm, never a live job — so this screen says so plainly
 * rather than implying the jobs are already out there. An owner who expects the
 * bench to fill by itself and finds drafts instead has been misled by the copy,
 * not the code.
 *
 * The two numbers on each row exist because **a sync that quietly stops working
 * looks exactly like a calendar with no bookings in it.** "Last read" and the
 * booking count are how somebody tells those apart without reading a log.
 */
const HELP = {
  airbnb: 'Airbnb: Calendar → Availability → Sync calendars → Export calendar.',
  vrbo: 'Vrbo: Calendar → Import/Export → Export calendar.',
}

function Feed({ feed, propertyId, onChanged, timeZone }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function act(path, options) {
    setBusy(true)
    setError('')
    try {
      await apiFetch(path, options)
      await onChanged()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <li
      className={`rounded-lg border p-3 ${
        feed.last_error || feed.last_stale_kept > 0
          ? 'border-amber-300 bg-amber-50'
          : 'border-slate-200'
      }`}
      data-testid="calendar-feed"
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="font-medium">{feed.label}</p>
          {/* The URL is shown truncated: it is the owner's own secret, and
              seeing enough of it to recognise which listing it is beats
              hiding it entirely. */}
          <p className="mt-0.5 break-all text-xs text-slate-500">{feed.url}</p>
        </div>
        <div className="flex shrink-0 gap-2">
          <button
            type="button"
            className="btn-secondary"
            disabled={busy}
            onClick={() =>
              act(`/properties/${propertyId}/calendars/${feed.id}/sync`, {
                method: 'POST',
              })
            }
            data-testid="sync-calendar"
          >
            {busy ? 'Reading…' : 'Sync now'}
          </button>
          <button
            type="button"
            className="btn-secondary text-red-700"
            disabled={busy}
            onClick={() =>
              act(`/properties/${propertyId}/calendars/${feed.id}`, {
                method: 'DELETE',
              })
            }
          >
            Remove
          </button>
        </div>
      </div>

      <p className="mt-2 text-xs text-slate-500" data-testid="calendar-status">
        {feed.last_synced_at
          ? `Last read ${formatDateTime(feed.last_synced_at, timeZone)}`
          : 'Not read yet'}
        {feed.last_booking_count !== null &&
          ` · ${feed.last_booking_count} booking${
            feed.last_booking_count === 1 ? '' : 's'
          }`}
      </p>

      {feed.last_error && (
        <p className="mt-2 text-sm text-amber-800" data-testid="calendar-error">
          {feed.last_error}
        </p>
      )}

      {/* A booking vanished from a job somebody is already on. This is the one
          number on the row that needs a decision rather than a glance, and it
          is read from the stored value so the scheduled sync — the one nobody
          is watching — can raise it just as loudly as the button can. */}
      {feed.last_stale_kept > 0 && (
        <p className="mt-2 text-sm text-amber-800" data-testid="calendar-stale">
          {feed.last_stale_kept} job{feed.last_stale_kept === 1 ? '' : 's'} here no
          longer {feed.last_stale_kept === 1 ? 'has a booking' : 'have bookings'} in
          the calendar, but {feed.last_stale_kept === 1 ? 'it is' : 'they are'}{' '}
          already posted or booked — check whether you still need{' '}
          {feed.last_stale_kept === 1 ? 'that clean' : 'those cleans'}.
        </p>
      )}

      {error && (
        <div className="mt-2">
          <Alert>{error}</Alert>
        </div>
      )}
    </li>
  )
}

export default function CalendarFeeds({ propertyId, isHome }) {
  const timeZone = useTimeZone()

  const [feeds, setFeeds] = useState(null)
  const [url, setUrl] = useState('')
  const [label, setLabel] = useState('Airbnb')
  const [adding, setAdding] = useState(false)
  const [error, setError] = useState('')
  const [lastSync, setLastSync] = useState(null)

  async function load() {
    try {
      setFeeds(await apiFetch(`/properties/${propertyId}/calendars`))
    } catch (err) {
      setError(err.message)
    }
  }

  useEffect(() => {
    // A home has no guests checking out, so it has no booking calendar and
    // this panel has nothing to say.
    if (!isHome) load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [propertyId, isHome])

  async function add(event) {
    event.preventDefault()
    setAdding(true)
    setError('')
    try {
      const result = await apiFetch(`/properties/${propertyId}/calendars`, {
        method: 'POST',
        body: { url: url.trim(), label: label.trim() || 'Calendar' },
      })
      setUrl('')
      setLastSync(result)
      await load()
    } catch (err) {
      setError(err.message)
    } finally {
      setAdding(false)
    }
  }

  if (isHome) return null

  return (
    <div className="card mt-6" data-testid="calendar-feeds">
      <h2 className="font-semibold">Booking calendar</h2>
      <p className="mt-1 text-sm text-slate-600">
        Connect the calendar your listing already publishes and each checkout shows up
        as a <strong>draft turnover</strong> for you to post. Nothing goes out to
        cleaners until you say so.
      </p>

      {feeds?.length > 0 && (
        <ul className="mt-4 space-y-3">
          {feeds.map((feed) => (
            <Feed
              key={feed.id}
              feed={feed}
              propertyId={propertyId}
              onChanged={load}
              timeZone={timeZone}
            />
          ))}
        </ul>
      )}

      {lastSync && (
        <p className="mt-3 text-sm text-slate-700" data-testid="sync-summary">
          {lastSync.created > 0
            ? `${lastSync.created} draft${lastSync.created === 1 ? '' : 's'} ready to post.`
            : 'Nothing new to add.'}
          {lastSync.stale_but_kept > 0 && (
            <span className="ml-1 text-amber-800">
              {lastSync.stale_but_kept} booking
              {lastSync.stale_but_kept === 1 ? ' is' : 's are'} gone from the calendar but
              already posted — check whether you still need {lastSync.stale_but_kept === 1 ? 'that clean' : 'those cleans'}.
            </span>
          )}
        </p>
      )}

      <form onSubmit={add} className="mt-4 space-y-3">
        <div className="grid gap-3 sm:grid-cols-3">
          <div className="sm:col-span-2">
            <label htmlFor="calendar-url" className="field-label">
              Export link
            </label>
            <input
              id="calendar-url"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://www.airbnb.com/calendar/ical/…"
              className="field-input"
              data-testid="calendar-url"
            />
          </div>
          <div>
            <label htmlFor="calendar-label" className="field-label">
              Name it
            </label>
            <input
              id="calendar-label"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              className="field-input"
              data-testid="calendar-label"
            />
          </div>
        </div>

        <p className="text-xs text-slate-500">
          {HELP.airbnb} {HELP.vrbo}
        </p>

        <button
          type="submit"
          className="btn-primary"
          disabled={adding || url.trim().length === 0}
          data-testid="add-calendar"
        >
          {adding ? 'Reading it…' : 'Connect calendar'}
        </button>
      </form>

      {error && (
        <div className="mt-3">
          <Alert>{error}</Alert>
        </div>
      )}
    </div>
  )
}
