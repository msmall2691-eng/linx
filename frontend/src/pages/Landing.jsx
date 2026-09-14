import { Link } from 'react-router-dom'
import { useEffect, useState } from 'react'

import { CleanerPreview, OwnerPreview } from '../components/ProductPreview.jsx'
import { apiFetch } from '../lib/api.js'

/**
 * The front door.
 *
 * **Written for property owners, with a real door for cleaners.** A two-sided
 * marketplace landing page that hedges between both audiences says nothing to
 * either, so this one picks a side: owners are the ones with the problem that
 * has a date on it. Cleaners get their own section, their own preview and their
 * own call to action rather than a smaller share of the same paragraph.
 *
 * Everything claimed here is something the product does today. No invented
 * volume, no "trusted by hundreds" — the first cleaners to sign up will find
 * out immediately, and a promise the product breaks on day one is worse than a
 * quieter one it keeps.
 */
function Step({ number, title, children }) {
  return (
    <li className="flex gap-4">
      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-brand-100 text-sm font-bold text-brand-700">
        {number}
      </span>
      <div>
        <h3 className="font-semibold">{title}</h3>
        <p className="mt-1 text-sm text-slate-600">{children}</p>
      </div>
    </li>
  )
}

export default function Landing() {
  const [region, setRegion] = useState(null)

  useEffect(() => {
    apiFetch('/config', { auth: false })
      .then((config) => setRegion(config.region_name))
      .catch(() => setRegion(null))
  }, [])

  return (
    <div>
      {/* --- hero: owners first --- */}
      <section className="mx-auto max-w-6xl px-4 pb-8 pt-16">
        <div className="grid items-center gap-10 lg:grid-cols-2">
          <div>
            <p className="text-sm font-semibold uppercase tracking-wide text-brand-600">
              {region ? `Now serving ${region}` : 'Short-term rental turnovers'}
            </p>
            <h1 className="mt-3 text-4xl font-bold tracking-tight sm:text-5xl">
              Your guests leave at 11. The next ones arrive at 4.
            </h1>
            <p className="mt-4 text-lg text-slate-600">
              Post the turnover. Vetted local cleaners name their price. You pick one,
              and you pay when the job is done — not before.
            </p>

            <div className="mt-8 flex flex-wrap gap-3">
              <Link to="/signup?role=owner" className="btn-primary" data-testid="owner-cta">
                Post your first turnover
              </Link>
              <Link to="/login" className="btn-secondary">
                Log in
              </Link>
            </div>

            <p className="mt-4 text-sm text-slate-500">
              Free to post. No subscription, no lead fees.
            </p>
          </div>

          <div className="lg:pl-6">
            <OwnerPreview />
          </div>
        </div>
      </section>

      {/* --- how it works, for owners --- */}
      <section className="border-y border-slate-200 bg-white">
        <div className="mx-auto max-w-6xl px-4 py-14">
          <h2 className="text-2xl font-bold tracking-tight">How it works</h2>
          <ol className="mt-8 grid gap-8 sm:grid-cols-3">
            <Step number="1" title="Post the window">
              Checkout time and the next checkin. The tighter the gap, the higher the
              job sits on every cleaner&rsquo;s board — a same-day turnaround is
              flagged as one.
            </Step>
            <Step number="2" title="Pick from the bids">
              Cleaners name their own price. You see their rating, whether their
              vetting is finished, and what they said — then you choose.
            </Step>
            <Step number="3" title="Pay when it&rsquo;s done">
              The card is charged when your cleaner marks the job complete. If they
              cancel, nothing was charged and the job goes straight back out.
            </Step>
          </ol>
        </div>
      </section>

      {/* --- trust, which is the actual objection --- */}
      <section className="mx-auto max-w-6xl px-4 py-14">
        <h2 className="text-2xl font-bold tracking-tight">
          Somebody is going into your house
        </h2>
        <p className="mt-2 max-w-2xl text-slate-600">
          That is the whole objection, so here is exactly what stands behind it.
        </p>

        <div className="mt-8 grid gap-6 sm:grid-cols-2 lg:grid-cols-4">
          <div className="card">
            <h3 className="font-semibold">A person checks the ID</h3>
            <p className="mt-2 text-sm text-slate-600">
              Not an algorithm. Someone looks at a photo ID and a reference before a
              cleaner can bid at all. It takes a day or two, deliberately.
            </p>
          </div>
          <div className="card">
            <h3 className="font-semibold">A real background check</h3>
            <p className="mt-2 text-sm text-slate-600">
              A photo ID confirms who somebody is, not what they have done. Both have
              to clear before anyone can bid on your place.
            </p>
          </div>
          <div className="card">
            <h3 className="font-semibold">Your address stays yours</h3>
            <p className="mt-2 text-sm text-slate-600">
              Cleaners bidding see the town and the job — not your street address,
              and never your gate code. Those appear when you hire someone.
            </p>
          </div>
          <div className="card">
            <h3 className="font-semibold">Reviews you can believe</h3>
            <p className="mt-2 text-sm text-slate-600">
              Neither side sees the other&rsquo;s review until both are written, so
              nobody writes a pre-emptive bad one to get ahead of yours.
            </p>
          </div>
        </div>
      </section>

      {/* --- the cleaner's door, not a footnote --- */}
      <section className="border-t border-slate-200 bg-slate-900 text-white">
        <div className="mx-auto max-w-6xl px-4 py-16">
          <div className="grid items-center gap-10 lg:grid-cols-2">
            <div>
              <p className="text-sm font-semibold uppercase tracking-wide text-brand-300">
                For cleaners
              </p>
              <h2 className="mt-3 text-3xl font-bold tracking-tight">
                Pick your own jobs. Name your own price.
              </h2>
              <p className="mt-4 text-lg text-slate-300">
                Turnovers near you, with the time, the size and what the owner budgeted
                — before you bid. No bidding wars on a lead you paid for, because you
                never pay for a lead.
              </p>

              <ul className="mt-6 space-y-2 text-slate-300">
                <li>· You set your own travel radius and your own prices.</li>
                <li>· Paid through Stripe when you mark the job done.</li>
                <li>· The platform&rsquo;s cut comes out of the price you set, and you
                  see it before you bid.</li>
                <li>· Vetting is a day or two, and one person reviews it.</li>
              </ul>

              <div className="mt-8">
                <Link
                  to="/signup?role=cleaner"
                  className="btn inline-flex bg-white text-slate-900 hover:bg-slate-100"
                  data-testid="cleaner-cta"
                >
                  Join the bench
                </Link>
              </div>
            </div>

            <div className="lg:pl-6">
              <CleanerPreview />
            </div>
          </div>
        </div>
      </section>

      <section className="mx-auto max-w-6xl px-4 py-14 text-center">
        <h2 className="text-2xl font-bold tracking-tight">
          {region ? `Built for ${region}` : 'Built for one region at a time'}
        </h2>
        <p className="mx-auto mt-3 max-w-xl text-slate-600">
          One region, so the cleaners on here are actually near you. If that is where
          your place is, this is for you.
        </p>
        <div className="mt-8 flex flex-wrap justify-center gap-3">
          <Link to="/signup?role=owner" className="btn-primary">
            I own a rental
          </Link>
          <Link to="/signup?role=cleaner" className="btn-secondary">
            I clean
          </Link>
        </div>
      </section>
    </div>
  )
}
