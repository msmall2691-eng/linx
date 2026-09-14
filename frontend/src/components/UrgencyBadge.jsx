// The urgency ladder is the product's core pricing and priority signal, so it
// renders the same way everywhere. The rung itself is decided on the server
// (app/services/urgency.py) — this only paints it.

const STYLES = {
  standard: 'bg-slate-100 text-urgency-standard',
  soon: 'bg-amber-100 text-urgency-soon',
  urgent: 'bg-red-100 text-urgency-urgent',
  same_day: 'bg-red-600 text-white',
}

const LABELS = {
  standard: 'Standard',
  soon: 'Soon',
  urgent: 'Urgent',
  same_day: 'Same day',
}

export default function UrgencyBadge({ urgency, className = '' }) {
  return (
    <span
      data-testid="urgency-badge"
      className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold ${
        STYLES[urgency] ?? STYLES.standard
      } ${className}`}
    >
      {LABELS[urgency] ?? urgency}
    </span>
  )
}
