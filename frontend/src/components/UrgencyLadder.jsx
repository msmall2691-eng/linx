/**
 * The urgency ladder, drawn.
 *
 * This is the product's core pricing and priority signal and the landing page
 * described it in a sentence, if at all. It is the one thing here that a
 * competitor's page does not have, so it is worth a picture.
 *
 * **The bar is the time left, so it gets shorter as the rung gets hotter.** A
 * bar that grew with urgency would read backwards — more bar, more time — and
 * this is a chart of exactly the thing that is running out.
 *
 * The colors are `urgency.*` from the Tailwind config, the same four the badges
 * use throughout the app, so the picture on the landing page and the badge on
 * the board cannot drift into different palettes.
 */
const RUNGS = [
  {
    key: 'standard',
    label: 'Standard',
    window: 'More than 3 days out',
    width: 'w-full',
    color: 'bg-urgency-standard',
    text: 'text-urgency-standard',
  },
  {
    key: 'soon',
    label: 'Soon',
    window: 'Under 72 hours',
    width: 'w-3/5',
    color: 'bg-urgency-soon',
    text: 'text-urgency-soon',
  },
  {
    key: 'urgent',
    label: 'Urgent',
    window: 'Under 24 hours',
    width: 'w-1/4',
    color: 'bg-urgency-urgent',
    text: 'text-urgency-urgent',
  },
  {
    key: 'same_day',
    label: 'Same day',
    // Only reachable for a rental: it describes a guest arriving the day
    // another leaves, which is not a thing that happens to a home.
    window: 'Guest out, guest in',
    width: 'w-[8%]',
    color: 'bg-urgency-sameday',
    text: 'text-urgency-sameday',
  },
]

export default function UrgencyLadder() {
  return (
    <ul className="space-y-4" data-testid="urgency-ladder">
      {RUNGS.map((rung) => (
        <li key={rung.key}>
          <div className="flex items-baseline justify-between gap-4">
            <span className={`text-sm font-semibold ${rung.text}`}>
              {rung.label}
            </span>
            <span className="text-sm text-slate-500">{rung.window}</span>
          </div>
          <span className="mt-1.5 block h-3 overflow-hidden rounded-full bg-slate-100">
            <span className={`block h-full rounded-full ${rung.width} ${rung.color}`} />
          </span>
        </li>
      ))}
    </ul>
  )
}
