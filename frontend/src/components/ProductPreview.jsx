import { useState } from 'react'

import UrgencyBadge from './UrgencyBadge.jsx'

/**
 * Small mock-ups of the real screens, for the landing page.
 *
 * **Built from the same components and classes as the product, not
 * screenshots.** A screenshot is a promise that goes stale the first time a
 * button moves, and nobody notices for a year. These use `UrgencyBadge` and the
 * same `card` styles the app does, so a change to either shows up here too.
 *
 * The numbers are illustrative and the page says so. That matters more than it
 * sounds: a marketplace landing page showing invented volume ("2,400 cleans
 * this month") is the kind of thing somebody signs up on and then feels lied
 * to about on day one, when they are the third cleaner in the region.
 */
function Frame({ label, children }) {
  return (
    <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
      <div className="flex items-center gap-1.5 border-b border-slate-200 bg-slate-50 px-3 py-2">
        <span className="h-2 w-2 rounded-full bg-slate-300" />
        <span className="h-2 w-2 rounded-full bg-slate-300" />
        <span className="h-2 w-2 rounded-full bg-slate-300" />
        <span className="ml-2 text-xs font-medium text-slate-500">{label}</span>
      </div>
      <div className="p-4">{children}</div>
    </div>
  )
}

export function OwnerPreview() {
  return (
    <Frame label="Your turnover">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="font-semibold">Lighthouse Cottage</p>
          <p className="mt-0.5 text-sm text-slate-600">
            Checkout Sat 11:00 am · checkin 4:00 pm
          </p>
        </div>
        <UrgencyBadge urgency="same_day" />
      </div>

      <p className="mt-4 text-xs font-medium uppercase tracking-wide text-slate-500">
        3 bids
      </p>
      <ul className="mt-2 space-y-2">
        {[
          { name: 'Kit M.', price: '$140', rating: '4.9', reviews: '12 reviews' },
          { name: 'Dana R.', price: '$155', rating: '5.0', reviews: '4 reviews' },
          { name: 'Sam T.', price: '$160', rating: null, reviews: 'No reviews yet' },
        ].map((bid) => (
          <li
            key={bid.name}
            className="flex items-center justify-between gap-3 rounded-lg border border-slate-200 px-3 py-2"
          >
            <div className="min-w-0">
              <p className="text-sm font-medium">{bid.name}</p>
              <p className="text-xs text-slate-500">
                {bid.rating && <span className="text-amber-500">★ {bid.rating} · </span>}
                {bid.reviews} · Vetting complete
              </p>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              <span className="text-sm font-semibold">{bid.price}</span>
              <span className="rounded-lg bg-brand-600 px-2 py-1 text-xs font-semibold text-white">
                Accept
              </span>
            </div>
          </li>
        ))}
      </ul>
    </Frame>
  )
}

export function CleanerPreview() {
  return (
    <Frame label="Open turnovers near you">
      <ul className="space-y-2">
        {[
          { where: 'Scarborough', when: 'Tomorrow, 11:00 am', miles: '6 mi', urgency: 'urgent', budget: '$150' },
          { where: 'Cape Elizabeth', when: 'Fri, 10:00 am', miles: '9 mi', urgency: 'soon', budget: '$175' },
          { where: 'Portland', when: 'Next Tue, 11:00 am', miles: '2 mi', urgency: 'standard', budget: '$130' },
        ].map((job) => (
          <li key={job.where} className="rounded-lg border border-slate-200 px-3 py-2">
            <div className="flex items-start justify-between gap-3">
              <div>
                <p className="text-sm font-medium">{job.where}, ME</p>
                <p className="text-xs text-slate-500">
                  {job.when} · {job.miles} away
                </p>
              </div>
              <UrgencyBadge urgency={job.urgency} />
            </div>
            <div className="mt-2 flex items-center justify-between gap-3">
              <span className="text-xs text-slate-500">
                Owner&rsquo;s budget {job.budget}
              </span>
              <span className="rounded-lg border border-slate-300 px-2 py-1 text-xs font-semibold text-slate-700">
                Place bid
              </span>
            </div>
          </li>
        ))}
      </ul>
      <p className="mt-3 text-xs text-slate-500">
        {/* The board deliberately shows the town, not the street address —
            a cleaner who has bid has not been hired. Saying so on the landing
            page is a feature, not a caveat. */}
        Street address and gate code appear once you&rsquo;re hired.
      </p>
    </Frame>
  )
}

/**
 * A home's job, which is a different card rather than the same card with a
 * field missing.
 *
 * A home has no next guest, so there is no window and no `same_day` rung — the
 * urgency is read on how soon the clean itself is due, which is the same
 * measure a standing vacancy uses. The scope is the thing a rental's card does
 * not carry at all: a standard clean and a move-out are different jobs, and a
 * cleaner pricing one needs to know which.
 */
export function HomePreview() {
  return (
    <Frame label="Your home clean">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="font-semibold">Maple Street</p>
          <p className="mt-0.5 text-sm text-slate-600">
            Clean due Thu 10:00 am
          </p>
        </div>
        <UrgencyBadge urgency="soon" />
      </div>

      <p className="mt-3 inline-flex rounded-lg bg-slate-100 px-2 py-1 text-xs font-medium text-slate-700">
        Deep clean · 3 bed · 1,400 sq ft
      </p>

      <p className="mt-4 text-xs font-medium uppercase tracking-wide text-slate-500">
        2 bids
      </p>
      <ul className="mt-2 space-y-2">
        {[
          { name: 'Rowan P.', price: '$210', rating: '4.8', reviews: '9 reviews' },
          { name: 'Alex D.', price: '$235', rating: '5.0', reviews: '3 reviews' },
        ].map((bid) => (
          <li
            key={bid.name}
            className="flex items-center justify-between gap-3 rounded-lg border border-slate-200 px-3 py-2"
          >
            <div className="min-w-0">
              <p className="text-sm font-medium">{bid.name}</p>
              <p className="text-xs text-slate-500">
                <span className="text-amber-500">★ {bid.rating} · </span>
                {bid.reviews} · Vetting complete
              </p>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              <span className="text-sm font-semibold">{bid.price}</span>
              <span className="rounded-lg bg-brand-600 px-2 py-1 text-xs font-semibold text-white">
                Accept
              </span>
            </div>
          </li>
        ))}
      </ul>
    </Frame>
  )
}

/**
 * The hero's preview, with a switch between the two kinds of job.
 *
 * **This is how the page is universal without hedging.** The alternative was
 * more words — a headline trying to name a rental and a home at once, which
 * names neither, or a second section most people never scroll to. A switch
 * shows both in the same square inch and lets somebody pick the one that is
 * theirs, which is the question they actually arrived with.
 *
 * Both are real shapes the product produces. A home has no next guest, so its
 * card has no window and no `same_day` rung, and it carries the scope of work
 * a rental's does not — which is the whole difference, shown rather than
 * explained.
 */
export function SwitchablePreview() {
  const [kind, setKind] = useState('rental')

  return (
    <div>
      <div
        className="mb-3 inline-flex rounded-lg bg-slate-100 p-1"
        role="group"
        aria-label="Kind of place"
      >
        {[
          { value: 'rental', label: 'Short-term rental' },
          { value: 'home', label: 'A home' },
        ].map((option) => (
          <button
            key={option.value}
            type="button"
            onClick={() => setKind(option.value)}
            aria-pressed={kind === option.value}
            data-testid={`preview-${option.value}`}
            className={`rounded-md px-3 py-1.5 text-sm font-medium transition ${
              kind === option.value
                ? 'bg-white text-ink shadow-sm'
                : 'text-slate-500 hover:text-slate-700'
            }`}
          >
            {option.label}
          </button>
        ))}
      </div>
      {kind === 'rental' ? <OwnerPreview /> : <HomePreview />}
    </div>
  )
}
