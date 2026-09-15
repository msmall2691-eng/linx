import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'

import Alert from '../components/Alert.jsx'
import { apiFetch } from '../lib/api.js'
import { useMaxBulkJobs, useTimeZone } from '../lib/config.jsx'
import {
  dollarsToCents,
  formatDateTime,
  isoToZonedInput,
  zonedInputToISO,
} from '../lib/datetime.js'

// A home's clean can be one of three things; a rental's is always a turnover.
// Offering a rental the choice would be a form that lies, since the server
// refuses a mismatch rather than correcting it.
const HOME_SCOPES = [
  { value: 'standard', label: 'Standard clean' },
  { value: 'deep', label: 'Deep clean' },
  { value: 'move_out', label: 'Move-out clean' },
]

/**
 * Read a pasted block of dates into `datetime-local` values.
 *
 * Deliberately forgiving about *format* and not at all forgiving about
 * *meaning*: anything it cannot read becomes a named problem rather than a
 * silently dropped line, because a line that vanishes from a paste of forty is
 * a job the owner thinks they scheduled.
 *
 * One date per line. A time may follow it; without one the property's own
 * default checkout time is used, which is the same thing the calendar feed
 * does with an all-day booking.
 */
export function parsePastedDates(text) {
  const rows = []
  const problems = []

  text
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .forEach((line, index) => {
      // Strip anything after a comma or tab so a pasted spreadsheet column
      // with a note beside it still reads.
      const [head] = line.split(/[,\t]/)
      const cleaned = head.trim()
      const match = cleaned.match(
        /^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?$/,
      )
      if (!match) {
        problems.push({ line: index + 1, text: line })
        return
      }
      const [, y, m, d, hh, mm] = match
      const pad = (value) => String(value).padStart(2, '0')
      rows.push({
        date: `${y}-${pad(m)}-${pad(d)}`,
        time: hh ? `${pad(hh)}:${mm}` : null,
      })
    })

  return { rows, problems }
}

/**
 * Add several jobs to one property at once.
 *
 * **For the owner a calendar feed cannot serve.** A home has no booking
 * calendar — that is what a home is — and a rental booked direct or by phone
 * has no `.ics` URL to paste. Both were left typing one job per screen, which
 * is fine for one job and absurd for a season.
 *
 * Everything lands as a draft. That is not a setting on this screen and
 * deliberately so: an owner who wanted these on the bench can post them from
 * the list in a minute, and an owner who pasted the wrong column cannot unsend
 * the alerts, the bids, or the apology.
 */
export default function TurnoverBulkNew() {
  const navigate = useNavigate()
  const timeZone = useTimeZone()
  const maxJobs = useMaxBulkJobs()
  const [params] = useSearchParams()

  const [properties, setProperties] = useState(null)
  const [propertyId, setPropertyId] = useState(params.get('property') || '')
  const [rows, setRows] = useState([])
  const [pasted, setPasted] = useState('')
  const [pasteProblems, setPasteProblems] = useState([])
  const [cleared, setCleared] = useState(0)
  // **The dates that did not fit, kept rather than discarded.**
  //
  // Capping alone was not enough and I claimed otherwise: `read-file` writes
  // nothing and knows nothing about what already exists, so uploading the same
  // calendar again returns the *same* full list, the cap takes the *same*
  // prefix, and every one of them comes back `already_there`. The remainder
  // was unreachable, while the screen told the owner to come back for it.
  const [overflow, setOverflow] = useState([])
  // Read inside an async completion, which closes over the *old* state value —
  // a ref is what makes "is this still the selected property" answerable there.
  const propertyIdRef = useRef(propertyId)
  useEffect(() => {
    propertyIdRef.current = propertyId
  }, [propertyId])
  const [scope, setScope] = useState('')
  const [budget, setBudget] = useState('')
  const [notes, setNotes] = useState('')
  const [error, setError] = useState(null)
  const [result, setResult] = useState(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    apiFetch('/properties')
      .then((list) => {
        const live = list.filter((p) => p.is_active)
        setProperties(live)
        if (!propertyId && live.length === 1) setPropertyId(live[0].id)
      })
      .catch((err) => setError(err.message))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const property = useMemo(
    () => properties?.find((p) => p.id === propertyId) || null,
    [properties, propertyId],
  )
  const isHome = property?.property_type === 'residential'

  // The default checkout time is the house's own policy — the same field the
  // calendar feed leans on, because an export says "the 7th" and the urgency
  // ladder is measured in hours.
  const defaultTime = (property?.default_checkout_time || '10:00').slice(0, 5)

  function addPasted() {
    const { rows: parsed, problems } = parsePastedDates(pasted)
    setPasteProblems(problems)
    if (parsed.length === 0) return
    setCleared(0)
    setRows((current) => {
      const room = Math.max(0, maxJobs - current.length)
      const taken = parsed.slice(0, room)
      setOverflow((waiting) => [
        ...waiting,
        ...parsed.slice(room).map((row) => ({
          id: `${row.date}-${Math.random().toString(36).slice(2, 8)}`,
          checkout: `${row.date}T${row.time || defaultTime}`,
          checkin: '',
        })),
      ])
      return [
        ...current,
        ...taken.map((row) => ({
          id: `${row.date}-${Math.random().toString(36).slice(2, 8)}`,
          checkout: `${row.date}T${row.time || defaultTime}`,
          checkin: '',
        })),
      ]
    })
    setPasted('')
  }

  async function readFile(event) {
    const file = event.target.files?.[0]
    if (!file || !propertyId) return
    setError(null)
    // **Whose calendar this was.** Parsing is a round trip, and the owner can
    // change the property while it is in flight — at which point appending the
    // answer would put one property's bookings on another's list, silently,
    // after the switch handler had already cleared the rows for exactly that
    // reason. Rental to rental would create drafts for the wrong house; rental
    // to home would restore checkins the screen no longer shows.
    const askedFor = propertyId
    const body = new FormData()
    body.append('file', file)
    try {
      const answer = await apiFetch(
        `/properties/${askedFor}/calendars/read-file`,
        { method: 'POST', body },
      )
      if (askedFor !== propertyIdRef.current) return
      if (answer.jobs.length === 0) {
        // `bookings_seen` is why this is not just an empty list: a file of last
        // year's stays is read and then dropped by the past floor, and saying
        // nothing would look like a broken upload rather than an old calendar.
        setError(
          answer.bookings_seen > 0
            ? `That calendar has ${answer.bookings_seen} booking(s), but none of them are upcoming cleans.`
            : 'That calendar has no bookings in it.',
        )
        return
      }
      setCleared(0)
      setRows((current) => {
        // **Never build a list the server will refuse.** A calendar can carry
        // more departures inside the sync horizon than one submission may
        // create — short stays reach that easily — and `create_many` rejects
        // the whole list rather than part of it, so the owner would have been
        // left deleting rows by hand with no idea how many to remove.
        //
        // Capped, and the remainder is not lost: submitting these and
        // uploading the same file again proposes the rest, and the ones
        // already created come back as `already_there` rather than twice.
        const room = Math.max(0, maxJobs - current.length)
        const taken = answer.jobs.slice(0, room)
        setOverflow((waiting) => [
          ...waiting,
          ...answer.jobs.slice(room).map((job, index) => ({
            id: `over-${index}-${Math.random().toString(36).slice(2, 8)}`,
            checkout: isoToZonedInput(job.checkout_at, timeZone),
            checkin: job.checkin_at ? isoToZonedInput(job.checkin_at, timeZone) : '',
          })),
        ])
        return [
          ...current,
          ...taken.map((job, index) => ({
            id: `file-${index}-${Math.random().toString(36).slice(2, 8)}`,
            checkout: isoToZonedInput(job.checkout_at, timeZone),
            checkin: job.checkin_at ? isoToZonedInput(job.checkin_at, timeZone) : '',
          })),
        ]
      })
    } catch (err) {
      if (askedFor === propertyIdRef.current) setError(err.message)
    } finally {
      // Let the same file be chosen again after a correction.
      event.target.value = ''
    }
  }

  function updateRow(id, field, value) {
    setRows((current) =>
      current.map((row) => (row.id === id ? { ...row, [field]: value } : row)),
    )
  }

  function removeRow(id) {
    setRows((current) => current.filter((row) => row.id !== id))
  }

  async function submit(event) {
    event.preventDefault()
    setError(null)

    // Refused, not dropped. `dollarsToCents` answers null for "abc" and for
    // "12.345" alike, and sending that as "no budget" would create every draft
    // without the number the owner typed — the single-job form corrects them,
    // and a bulk form that quietly disagreed would be worse, not better.
    const budgetCents = dollarsToCents(budget)
    if (budget !== '' && budgetCents === null) {
      setError('Enter the budget as dollars and cents, like 125 or 125.50.')
      return
    }

    setBusy(true)
    try {
      const answer = await apiFetch('/turnovers/bulk', {
        method: 'POST',
        body: {
          property_id: propertyId,
          jobs: rows.map((row) => ({
            checkout_at: zonedInputToISO(row.checkout, timeZone),
            checkin_at: row.checkin ? zonedInputToISO(row.checkin, timeZone) : null,
          })),
          service_type: scope || null,
          owner_budget_cents: budgetCents,
          notes: notes || null,
        },
      })
      setResult(answer)
      setRows([])
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  if (result) {
    return (
      <div className="mx-auto max-w-2xl" data-testid="bulk-result">
        <div className="card">
          <h1 className="text-xl font-semibold">
            {result.created.length} draft{result.created.length === 1 ? '' : 's'} added
          </h1>
          <p className="mt-2 text-sm text-slate-600">
            Nobody has been told about these yet — they are drafts. Post the ones
            you want on the bench from the jobs list.
          </p>
          {result.already_there.length > 0 && (
            <Alert kind="info" className="mt-4">
              {result.already_there.length} date
              {result.already_there.length === 1 ? ' was' : 's were'} skipped
              because this property already has a job then:{' '}
              {result.already_there
                .map((iso) => formatDateTime(iso, timeZone))
                .join('; ')}
              .
            </Alert>
          )}
          {overflow.length > 0 && (
            <Alert kind="info" className="mt-4">
              {overflow.length}{' '}
              {overflow.length === 1 ? 'date' : 'dates'} from that list did not
              fit in one submission and {overflow.length === 1 ? 'is' : 'are'}{' '}
              still waiting.
            </Alert>
          )}
          <div className="mt-6 flex flex-wrap gap-3">
            {overflow.length > 0 && (
              <button
                type="button"
                className="btn-primary"
                data-testid="add-waiting"
                onClick={() => {
                  // Straight into the form, so the remainder never depends on
                  // the owner still having the file — or on re-uploading it,
                  // which proposes the same prefix and creates nothing.
                  setRows(overflow.slice(0, maxJobs))
                  setOverflow(overflow.slice(maxJobs))
                  setResult(null)
                }}
              >
                Add the next {Math.min(overflow.length, maxJobs)}
              </button>
            )}
            <Link className="btn-primary" to="/turnovers">
              Go to jobs
            </Link>
            <button
              type="button"
              className="btn-secondary"
              onClick={() => setResult(null)}
            >
              Add more
            </button>
          </div>
        </div>
      </div>
    )
  }

  // An owner with nothing to add jobs *to* — no properties yet, or every one
  // archived — would otherwise get a required select with one permanently
  // empty option and no route onward. The single-job form has said this for
  // as long as it has existed; this is the same sentence.
  if (properties !== null && properties.length === 0) {
    return (
      <div className="mx-auto max-w-2xl" data-testid="bulk-no-properties">
        <h1 className="text-xl font-semibold">Add several jobs at once</h1>
        <p className="mt-3 text-slate-600">
          Add a property first — a job is a cleaning at one of your places. If
          you have archived them all, restore one and it will appear here.
        </p>
        <div className="mt-6 flex gap-3">
          <Link to="/properties/new" className="btn-primary">
            Add a property
          </Link>
          <Link to="/properties" className="btn-secondary">
            See your properties
          </Link>
        </div>
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-3xl" data-testid="bulk-new">
      <div className="mb-4">
        <h1 className="text-xl font-semibold">Add several jobs at once</h1>
        <p className="mt-1 text-sm text-slate-600">
          For when there is no booking calendar to connect — a home never has
          one, and plenty of rentals are booked direct. Everything you add here
          starts as a draft.
        </p>
      </div>

      {error && (
        <Alert kind="error" className="mb-4">
          {error}
        </Alert>
      )}

      <form onSubmit={submit} className="card space-y-5">
        <div>
          <label className="label" htmlFor="property">
            Property
          </label>
          <select
            id="property"
            className="input"
            value={propertyId}
            onChange={(e) => {
              // **Rows belong to the property they were entered for.** Their
              // times came from that property's default checkout hour, and a
              // rental's rows may carry checkins — which a home refuses as a
              // category error. Kept across a switch, those checkins stay in
              // state while the screen stops showing the field, so the server
              // refuses the whole batch over a value the owner cannot see or
              // repair. Cleared, and said out loud rather than silently.
              if (rows.length > 0 && e.target.value !== propertyId) {
                setCleared(rows.length)
              }
              setRows([])
              setPasteProblems([])
              setOverflow([])
              setPropertyId(e.target.value)
              setScope('')
            }}
            required
          >
            <option value="">Choose a property…</option>
            {(properties || []).map((p) => (
              <option key={p.id} value={p.id}>
                {p.nickname}
              </option>
            ))}
          </select>
        </div>

        {propertyId && (
          <>
            <div className="rounded-lg border border-slate-200 p-4">
              <label className="label" htmlFor="pasted">
                Paste or type the dates
              </label>
              <p className="mb-2 text-xs text-slate-500">
                One per line — <code>2027-03-04</code>, or{' '}
                <code>2027-03-04 14:00</code> to set the time. Without a time,{' '}
                {defaultTime} is used.
              </p>
              <textarea
                id="pasted"
                className="input h-24 font-mono text-sm"
                value={pasted}
                onChange={(e) => setPasted(e.target.value)}
                placeholder={'2027-03-04\n2027-03-11\n2027-03-18'}
              />
              <div className="mt-2 flex flex-wrap items-center gap-3">
                <button type="button" className="btn-secondary" onClick={addPasted}>
                  Add these dates
                </button>
                {!isHome && (
                  <label className="cursor-pointer text-sm text-brand-700 underline">
                    <input
                      type="file"
                      accept=".ics,text/calendar"
                      className="hidden"
                      onChange={readFile}
                      data-testid="ics-upload"
                    />
                    or upload a calendar file (.ics)
                  </label>
                )}
              </div>
              {overflow.length > 0 && (
                <Alert kind="info" className="mt-3">
                  {overflow.length} more{' '}
                  {overflow.length === 1 ? 'date is' : 'dates are'} waiting —
                  one submission takes at most {maxJobs} jobs. Add these, and
                  the rest are offered straight afterwards.
                </Alert>
              )}
              {cleared > 0 && (
                <Alert kind="info" className="mt-3">
                  {cleared} row{cleared === 1 ? '' : 's'}{' '}
                  {cleared === 1 ? 'was' : 'were'} cleared, because dates belong
                  to the property they were entered for.
                </Alert>
              )}
              {pasteProblems.length > 0 && (
                <Alert kind="warning" className="mt-3">
                  {pasteProblems.length} line
                  {pasteProblems.length === 1 ? '' : 's'} could not be read and{' '}
                  {pasteProblems.length === 1 ? 'was' : 'were'} left out:{' '}
                  {pasteProblems.map((p) => `line ${p.line} (“${p.text}”)`).join('; ')}
                  .
                </Alert>
              )}
            </div>

            {rows.length > 0 && (
              <div data-testid="bulk-rows">
                <p className="label">
                  {rows.length} job{rows.length === 1 ? '' : 's'} to add
                </p>
                <ul className="mt-2 space-y-2">
                  {rows.map((row) => (
                    <li
                      key={row.id}
                      className="flex flex-wrap items-end gap-3 rounded-lg border border-slate-200 p-3"
                      data-testid="bulk-row"
                    >
                      <div className="grow">
                        <label className="text-xs text-slate-500">
                          {isHome ? 'Clean due' : 'Checkout'}
                        </label>
                        <input
                          type="datetime-local"
                          className="input"
                          value={row.checkout}
                          onChange={(e) =>
                            updateRow(row.id, 'checkout', e.target.value)
                          }
                          required
                        />
                      </div>
                      {/* A home has no next guest, so there is no field for one
                          — the server refuses a checkin there as a category
                          error, and offering the box would be a form that lies. */}
                      {!isHome && (
                        <div className="grow">
                          <label className="text-xs text-slate-500">
                            Next checkin (optional)
                          </label>
                          <input
                            type="datetime-local"
                            className="input"
                            value={row.checkin}
                            onChange={(e) =>
                              updateRow(row.id, 'checkin', e.target.value)
                            }
                          />
                        </div>
                      )}
                      <button
                        type="button"
                        className="btn-secondary"
                        onClick={() => removeRow(row.id)}
                      >
                        Remove
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            <div className="grid gap-4 sm:grid-cols-2">
              {isHome && (
                <div>
                  <label className="label" htmlFor="scope">
                    Scope of work
                  </label>
                  <select
                    id="scope"
                    className="input"
                    value={scope}
                    onChange={(e) => setScope(e.target.value)}
                  >
                    <option value="">Standard clean</option>
                    {HOME_SCOPES.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                </div>
              )}
              <div>
                <label className="label" htmlFor="budget">
                  Budget each (optional)
                </label>
                <input
                  id="budget"
                  className="input"
                  inputMode="decimal"
                  value={budget}
                  onChange={(e) => setBudget(e.target.value)}
                  placeholder="120"
                />
              </div>
            </div>

            <div>
              <label className="label" htmlFor="notes">
                Notes for the cleaner (optional)
              </label>
              <textarea
                id="notes"
                className="input h-20"
                value={notes}
                onChange={(e) => setNotes(e.target.value)}
              />
              <p className="mt-1 text-xs text-slate-500">
                These apply to every job you add here. Edit any one of them
                afterwards.
              </p>
            </div>

            <div className="flex items-center gap-3">
              <button
                type="submit"
                className="btn-primary"
                disabled={busy || rows.length === 0}
              >
                {busy ? 'Adding…' : `Add ${rows.length || ''} as drafts`.trim()}
              </button>
              <button
                type="button"
                className="btn-secondary"
                onClick={() => navigate('/turnovers')}
              >
                Cancel
              </button>
            </div>
          </>
        )}
      </form>
    </div>
  )
}
