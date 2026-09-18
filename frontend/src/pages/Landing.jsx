import { Link } from 'react-router-dom'
import { useEffect, useState } from 'react'

import {
  CalendarPlus,
  CardCheck,
  IdCard,
  Key,
  PriceTag,
  ShieldCheck,
  TwoNotes,
} from '../components/Icons.jsx'
import { CleanerPreview, SwitchablePreview } from '../components/ProductPreview.jsx'
import UrgencyLadder from '../components/UrgencyLadder.jsx'
import { apiFetch } from '../lib/api.js'

/**
 * The front door.
 *
 * **One page for everybody who arrives at it**, which is three people: an owner
 * with a short-term rental, an owner with a home, and a cleaner. The earlier
 * version led with a rental — "your guests leave at 11" — and gave the home its
 * own section further down. That was a deliberate call and it was the wrong
 * one: it is an Airbnb page with an annex, and somebody whose place is a house
 * reads the first screen and leaves.
 *
 * The fix is not a longer headline naming both. It is **showing instead of
 * saying**: the hero preview switches between a rental's job and a home's, so
 * the page answers "is this for someone like me" with a picture in the time it
 * takes to read six words. A switch is smaller than a paragraph and says more.
 *
 * **Prose is the thing being cut.** Everything here that survived is either a
 * picture, a number, or a sentence that would cost somebody money if it were
 * missing. The trust cards went from three sentences each to one, because the
 * previous version explained the reasoning behind a policy on a page whose job
 * is to say the policy exists.
 *
 * Everything claimed here is still something the product does today. No
 * invented volume, no "trusted by hundreds" — the first cleaners to sign up
 * find out immediately, and a promise broken on day one is worse than a
 * quieter one kept.
 */
function Step({ icon: Icon, title, children }) {
  return (
    <li>
      <span className="inline-flex h-11 w-11 items-center justify-center rounded-xl bg-brand-50 text-brand-600">
        <Icon />
      </span>
      <h3 className="mt-3 font-semibold">{title}</h3>
      <p className="mt-1 text-sm text-slate-600">{children}</p>
    </li>
  )
}

function TrustCard({ icon: Icon, title, children }) {
  return (
    <div className="card">
      <span className="inline-flex h-10 w-10 items-center justify-center rounded-lg bg-brand-50 text-brand-600">
        <Icon className="h-5 w-5" />
      </span>
      <h3 className="mt-3 font-semibold">{title}</h3>
      <p className="mt-1 text-sm text-slate-600">{children}</p>
    </div>
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
      {/* --- hero: one headline, for anybody with a place and a date --- */}
      <section className="mx-auto max-w-6xl px-4 pb-10 pt-16">
        <div className="grid items-center gap-10 lg:grid-cols-2">
          <div>
            <p className="text-sm font-semibold uppercase tracking-wide text-brand-600">
              {region ? `Now serving ${region}` : 'Vetted local cleaners'}
            </p>
            <h1 className="mt-3 text-4xl font-bold tracking-tight sm:text-5xl">
              Somebody you&rsquo;d trust with your keys.
            </h1>
            <p className="mt-4 text-lg text-slate-600">
              Post the job. Vetted local cleaners bid. You pick one — and pay
              when it&rsquo;s done.
            </p>

            <div className="mt-8 flex flex-wrap gap-3">
              <Link to="/signup?role=owner" className="btn-primary" data-testid="owner-cta">
                Find a cleaner
              </Link>
              <Link
                to="/signup?role=cleaner"
                className="btn-secondary"
                data-testid="cleaner-cta"
              >
                Work as a cleaner
              </Link>
            </div>

            <p className="mt-4 text-sm text-slate-500">
              Free to post. No subscription, no lead fees.
            </p>
          </div>

          {/* The switch is the argument. A rental's job and a home's job, in
              the same square inch, so nobody has to be told which they are. */}
          <div className="lg:pl-6">
            <SwitchablePreview />
          </div>
        </div>
      </section>

      {/* --- how it works: three pictures, three lines --- */}
      <section className="border-y border-slate-200 bg-white">
        <div className="mx-auto max-w-6xl px-4 py-14">
          <h2 className="text-2xl font-bold tracking-tight">How it works</h2>
          <ol className="mt-8 grid gap-8 sm:grid-cols-3">
            <Step icon={CalendarPlus} title="Post the job">
              A turnover between guests, or a clean at home. You say when.
            </Step>
            <Step icon={PriceTag} title="Cleaners bid">
              They name their price. You see it next to their rating and their
              vetting.
            </Step>
            <Step icon={CardCheck} title="Pay when it’s done">
              Charged when your cleaner marks the job complete. Never before.
            </Step>
          </ol>
        </div>
      </section>

      {/* --- the urgency ladder, which is the thing nobody else has --- */}
      <section className="mx-auto max-w-6xl px-4 py-14">
        <div className="grid gap-10 lg:grid-cols-[1fr_1.4fr] lg:items-center">
          <div>
            <h2 className="text-2xl font-bold tracking-tight">
              The tightest jobs rise to the top
            </h2>
            <p className="mt-3 text-slate-600">
              Every job is ranked by how little time is left on it. A cleaner
              opening the board sees the desperate ones first — which is why
              posting late still gets answered.
            </p>
          </div>
          <UrgencyLadder />
        </div>
      </section>

      {/* --- trust: four icons, four lines --- */}
      <section className="border-t border-slate-200 bg-white">
        <div className="mx-auto max-w-6xl px-4 py-14">
          <h2 className="text-2xl font-bold tracking-tight">
            Somebody is going into your place
          </h2>
          <p className="mt-2 text-slate-600">
            A rental or the house you live in — the line is the same.
          </p>

          <div className="mt-8 grid gap-6 sm:grid-cols-2 lg:grid-cols-4">
            <TrustCard icon={IdCard} title="A person checks the ID">
              Not an algorithm. A human reviews a photo ID and a reference
              before anyone can bid.
            </TrustCard>
            <TrustCard icon={ShieldCheck} title="A real background check">
              An ID says who somebody is, not what they&rsquo;ve done. Both have
              to clear.
            </TrustCard>
            <TrustCard icon={Key} title="Your address stays yours">
              Cleaners bidding see the town, not your street — and never your
              gate code.
            </TrustCard>
            <TrustCard icon={TwoNotes} title="Reviews you can believe">
              Neither side sees the other&rsquo;s until both are written.
            </TrustCard>
          </div>
        </div>
      </section>

      {/* --- the cleaner's door --- */}
      <section className="border-t border-slate-200 bg-slate-900 text-white">
        <div className="mx-auto max-w-6xl px-4 py-16">
          <div className="grid items-center gap-10 lg:grid-cols-2">
            <div>
              <p className="text-sm font-semibold uppercase tracking-wide text-brand-100">
                For cleaners
              </p>
              <h2 className="mt-3 text-3xl font-bold tracking-tight">
                Pick your own jobs. Name your own price.
              </h2>
              <p className="mt-4 text-lg text-slate-300">
                Rental turnovers and home cleans near you, with the time, the
                size and the owner&rsquo;s budget — before you bid.
              </p>

              <ul className="mt-6 space-y-2 text-slate-300">
                <li>· You set your travel radius and your prices.</li>
                <li>· Paid through Stripe when you mark the job done.</li>
                <li>· You never pay for a lead.</li>
              </ul>

              <div className="mt-8">
                <Link
                  to="/signup?role=cleaner"
                  className="btn inline-flex bg-white text-slate-900 hover:bg-slate-100"
                  data-testid="cleaner-cta-footer"
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
        <p className="mx-auto mt-3 max-w-md text-slate-600">
          One region, so the cleaners on here are actually near you.
        </p>
        <div className="mt-8 flex flex-wrap justify-center gap-3">
          <Link to="/signup?role=owner" className="btn-primary">
            Find a cleaner
          </Link>
          <Link to="/signup?role=cleaner" className="btn-secondary">
            Work as a cleaner
          </Link>
        </div>
      </section>
    </div>
  )
}
