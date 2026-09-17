import { Link } from 'react-router-dom'
import { useEffect, useState } from 'react'

import {
  CleanerPreview,
  HomePreview,
  OwnerPreview,
} from '../components/ProductPreview.jsx'
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
 * **There are three audiences, not two, and a home is the third.** Residential
 * was reopened, so an owner whose place is somebody's house can use all of
 * this — and read a hero about guests leaving at 11 and conclude, correctly on
 * the evidence in front of them, that it is an Airbnb product. The fix is the
 * same shape as the one for cleaners rather than a broader hero: a section of
 * their own, a preview of their own and a door of their own. A hedged hero
 * would cost the sharp case and still not name the home out loud. What the
 * hero does carry is a **signpost** — one line, above the fold, so nobody is
 * turned away before the section that is for them.
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
              {region ? `Now serving ${region}` : 'Rental turnovers and home cleans'}
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

            {/* The signpost, not a hedge. A home owner who reads the headline
                above has every reason to think this is an Airbnb product, and
                they would leave before reaching the section written for them. */}
            <p className="mt-2 text-sm text-slate-500">
              Not a rental?{' '}
              <a href="#homes" className="text-brand-700 underline" data-testid="homes-signpost">
                Homes get cleaned here too
              </a>
              .
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
            <Step number="1" title="Post the job">
              For a rental, the checkout and the next checkin — the tighter the gap,
              the higher it sits on every cleaner&rsquo;s board, and a same-day
              turnaround is flagged as one. For a home, simply when the clean is
              due, and the sooner it is, the higher it sits.
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

      {/* --- the home owner's door, the same shape as the cleaner's --- */}
      <section id="homes" className="border-b border-slate-200 bg-slate-50">
        <div className="mx-auto max-w-6xl px-4 py-16">
          <div className="grid items-center gap-10 lg:grid-cols-2">
            <div>
              <p className="text-sm font-semibold uppercase tracking-wide text-brand-600">
                For homes
              </p>
              <h2 className="mt-3 text-3xl font-bold tracking-tight">
                No guests. Just a house that needs cleaning.
              </h2>
              <p className="mt-4 text-lg text-slate-600">
                Same cleaners, same vetting, same pay-when-it&rsquo;s-done. A home has
                no checkout and nobody arriving at 4 — so you say when the clean is
                due, and that is the whole difference.
              </p>

              <ul className="mt-6 space-y-2 text-slate-600">
                <li>
                  · A standard clean, a deep clean, or a move-out — you pick, and the
                  cleaners bidding can see which.
                </li>
                <li>
                  · Square footage is optional. Plenty of people genuinely
                  don&rsquo;t know it, and a guessed number is worse than none.
                </li>
                {/* Recurring schedules are out of scope for v1, and this is the
                    one place somebody would assume otherwise. Saying it plainly
                    is cheaper than the support email, and `create_many` is a real
                    answer rather than an apology. */}
                <li>
                  · Nothing repeats on its own yet — you post the dates you want.
                  You can post a season of them in one go.
                </li>
              </ul>

              <div className="mt-8">
                <Link
                  to="/signup?role=owner"
                  className="btn-primary inline-flex"
                  data-testid="home-cta"
                >
                  Post a clean for your home
                </Link>
              </div>
            </div>

            <div className="lg:pl-6">
              <HomePreview />
            </div>
          </div>
        </div>
      </section>

      {/* --- trust, which is the actual objection --- */}
      <section className="mx-auto max-w-6xl px-4 py-14">
        <h2 className="text-2xl font-bold tracking-tight">
          Somebody is going into your house
        </h2>
        <p className="mt-2 max-w-2xl text-slate-600">
          That is the whole objection, so here is exactly what stands behind it.
          {/* The boundary does not soften for a home — it matters more, because
              somebody lives there. The section that follows is the same for
              both, and this is the sentence that says so out loud. */}{' '}
          None of it is different for a home. If anything it matters more there,
          because somebody lives in it.
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
                Turnovers and home cleans near you, with the time, the size and what
                the owner budgeted — before you bid. No bidding wars on a lead you
                paid for, because you never pay for a lead.
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
        {/* Two of these go to the same place, deliberately. The question the
            button answers is "is this for someone like me", which routing does
            not answer and a label does. */}
        <div className="mt-8 flex flex-wrap justify-center gap-3">
          <Link to="/signup?role=owner" className="btn-primary">
            I own a rental
          </Link>
          <Link to="/signup?role=owner" className="btn-primary" data-testid="home-owner-cta">
            I own a home
          </Link>
          <Link to="/signup?role=cleaner" className="btn-secondary">
            I clean
          </Link>
        </div>
      </section>
    </div>
  )
}
