// Times in this product are read and written in the region's local time, never
// the browser's.
//
// That distinction is not pedantic: short-term-rental owners are frequently not
// in the same timezone as the rental. A Portland cottage owned by someone in
// California, scheduled through the browser's local clock, would post an 11am
// checkout as 2pm — turning a comfortable turnaround into a same-day scramble,
// or hiding a genuine one. The urgency ladder is only as good as these
// timestamps, so the region's zone is the one that counts.
//
// The backend refuses naive timestamps outright for the same reason.

/** Milliseconds that `timeZone` is offset from UTC at a given instant. */
function offsetMsAt(date, timeZone) {
  // Format the instant in the target zone, then read it back as if it were UTC.
  // The difference between that and the true instant is the zone's offset.
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone,
    hour12: false,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
    .formatToParts(date)
    .reduce((acc, part) => {
      if (part.type !== 'literal') acc[part.type] = part.value
      return acc
    }, {})

  // hour can come back as "24" at midnight in some engines.
  const hour = parts.hour === '24' ? '00' : parts.hour
  const asUTC = Date.UTC(
    Number(parts.year),
    Number(parts.month) - 1,
    Number(parts.day),
    Number(hour),
    Number(parts.minute),
    Number(parts.second),
  )
  return asUTC - date.getTime()
}

/**
 * Turn a `<input type="datetime-local">` value into an ISO instant, reading it
 * as a wall-clock time in `timeZone`.
 *
 * @param {string} localValue e.g. "2026-07-01T11:00"
 * @param {string} timeZone   IANA zone, e.g. "America/New_York"
 * @returns {string|null} ISO 8601 with a real offset, or null if unparseable
 */
export function zonedInputToISO(localValue, timeZone) {
  if (!localValue) return null

  const naiveAsUTC = new Date(`${localValue}:00Z`)
  if (Number.isNaN(naiveAsUTC.getTime())) return null

  // Subtract the offset to get a candidate instant, then re-read the offset at
  // that instant. The second pass matters on the two days a year when the
  // offset changes: the first guess can land on the wrong side of the shift.
  const firstGuess = new Date(naiveAsUTC.getTime() - offsetMsAt(naiveAsUTC, timeZone))
  const corrected = new Date(naiveAsUTC.getTime() - offsetMsAt(firstGuess, timeZone))
  return corrected.toISOString()
}

/**
 * Turn an ISO instant into a `datetime-local` input value showing the region's
 * wall-clock time — the inverse of `zonedInputToISO`, for editing.
 */
export function isoToZonedInput(iso, timeZone) {
  if (!iso) return ''
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''

  const shifted = new Date(date.getTime() + offsetMsAt(date, timeZone))
  return shifted.toISOString().slice(0, 16)
}

/** "Wed, Jul 1, 11:00 AM" in the region's zone. */
export function formatDateTime(iso, timeZone) {
  if (!iso) return '—'
  return new Intl.DateTimeFormat('en-US', {
    timeZone,
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  }).format(new Date(iso))
}

/** "Jul 1" in the region's zone. */
export function formatDate(iso, timeZone) {
  if (!iso) return '—'
  return new Intl.DateTimeFormat('en-US', {
    timeZone,
    month: 'short',
    day: 'numeric',
  }).format(new Date(iso))
}

/** How long the cleaner actually has, in plain words. */
export function formatTurnaround(checkoutISO, checkinISO) {
  if (!checkinISO) return 'No next guest booked'

  const hours = (new Date(checkinISO) - new Date(checkoutISO)) / 36e5
  if (hours < 1) return 'Under an hour between guests'
  if (hours < 24) return `${Math.round(hours)} hours between guests`

  const days = hours / 24
  const rounded = days < 10 ? Math.round(days * 10) / 10 : Math.round(days)
  return `${rounded} day${rounded === 1 ? '' : 's'} between guests`
}

/** Money is integer cents everywhere. Never a float. */
export function formatCents(cents) {
  if (cents === null || cents === undefined) return '—'
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: cents % 100 === 0 ? 0 : 2,
  }).format(cents / 100)
}

/**
 * Dollars typed by a person into integer cents, without float drift.
 *
 * **Null is the only "no" this returns**, which it did not always manage. A
 * lone `.` passes the shape test — `\d*` matches nothing, `\.?` matches the
 * dot, `\d{0,2}` matches nothing — and `Number('.')` is NaN, so this handed
 * back NaN while every caller tested `=== null`. `JSON.stringify` then turns
 * NaN into `null` on the wire, so a budget somebody typed became no budget at
 * all, silently, with no correction offered. Both forms tested for null and
 * both were wrong in the same way, which is why the fix is here rather than at
 * the call sites.
 */
export function dollarsToCents(value) {
  if (value === '' || value === null || value === undefined) return null
  const cleaned = String(value).replace(/[$,\s]/g, '')
  if (!/^\d*\.?\d{0,2}$/.test(cleaned)) return null
  const cents = Math.round(Number(cleaned) * 100)
  return Number.isFinite(cents) ? cents : null
}
