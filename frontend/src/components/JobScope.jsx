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
