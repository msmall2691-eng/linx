/**
 * What kind of clean, and what kind of place — the two facts a cleaner needs
 * before naming a price.
 *
 * A turnover and a move-out are both "a clean" and are not the same job: one is
 * two hours, the other is most of a day. Somebody's occupied home is a
 * different job again from an empty rental between guests. A cleaner who cannot
 * tell them apart before bidding prices one of them wrong, and the ones who
 * guess wrong stop bidding.
 *
 * The labels live here so the board, the job card and the owner's page all read
 * the same words for the same thing. The *rule* about which scope belongs to
 * which property is the server's (`app/services/turnovers.py`) — this only
 * paints what came back.
 */
const SCOPE_LABELS = {
  turnover: 'Turnover',
  standard: 'Standard clean',
  deep: 'Deep clean',
  move_out: 'Move-out clean',
}

const SCOPE_STYLES = {
  turnover: 'bg-slate-100 text-slate-700',
  standard: 'bg-sky-100 text-sky-800',
  deep: 'bg-violet-100 text-violet-800',
  move_out: 'bg-orange-100 text-orange-800',
}

export function ScopeBadge({ serviceType, className = '' }) {
  if (!serviceType) return null
  return (
    <span
      data-testid="scope-badge"
      className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold ${
        SCOPE_STYLES[serviceType] ?? SCOPE_STYLES.turnover
      } ${className}`}
    >
      {SCOPE_LABELS[serviceType] ?? serviceType}
    </span>
  )
}

/** Beds, baths, square feet and whether somebody lives there. */
export function PropertySpecs({ property, className = '' }) {
  if (!property) return null

  const parts = [
    `${property.bedrooms} bd`,
    `${Number(property.bathrooms)} ba`,
    // Only when the owner knew it — an absent number says "not sure", which is
    // more honest than a guess and reads fine as a missing item in a list.
    property.square_feet ? `${property.square_feet.toLocaleString()} sq ft` : null,
    property.property_type === 'residential' ? 'Occupied home' : null,
  ].filter(Boolean)

  return (
    <p className={`text-sm text-slate-600 ${className}`} data-testid="property-specs">
      {parts.join(' · ')}
    </p>
  )
}

/**
 * The timing of a job, in the words that match the kind of place it is.
 *
 * **A home does not have a next guest**, so a card that says "Next checkin:
 * None booked" under a scope badge reading "Deep clean" is telling somebody
 * their house is an empty rental between bookings. The fields are not just
 * empty — they are the wrong question.
 *
 * One component so the wording has one author. The board, the cleaner's job
 * card and the owner's detail page all render this, and none of them carries
 * its own idea of what a checkin means.
 *
 * Returns the `<div>` items for an existing `<dl>`, so each page keeps its own
 * grid.
 */
function Field({ label, children, className = '' }) {
  return (
    <div className={className}>
      <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</dt>
      <dd className="mt-0.5 text-sm">{children}</dd>
    </div>
  )
}

export function JobSchedule({ job, propertyType, timeZone, formatDateTime, formatTurnaround }) {
  const isHome = propertyType === 'residential'

  if (isHome) {
    // One field, because there is exactly one time that matters. No window to
    // measure and no second booking to measure it against.
    return (
      <Field label="Scheduled for" className="sm:col-span-2">
        {formatDateTime(job.checkout_at, timeZone)}
      </Field>
    )
  }

  return (
    <>
      <Field label="Checkout">{formatDateTime(job.checkout_at, timeZone)}</Field>
      <Field label="Next checkin">
        {job.checkin_at ? (
          formatDateTime(job.checkin_at, timeZone)
        ) : (
          <span className="text-slate-400">None booked yet</span>
        )}
      </Field>
      <Field label="The window" className="sm:col-span-2">
        {formatTurnaround(job.checkout_at, job.checkin_at)}
      </Field>
    </>
  )
}
