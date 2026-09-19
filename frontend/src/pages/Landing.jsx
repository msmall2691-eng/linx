import { Link } from 'react-router-dom'
import { useEffect, useState } from 'react'

import {
  CalendarPlus,
  CardCheck,
  Chat,
  Clock,
  IdCard,
  Key,
  PriceTag,
  Route,
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
 * with a short-term rental, an owner with a home, and a cleaner. An earlier
 * version led with a rental and gave the home a section further down, which is
 * an Airbnb page with an annex — the hero is the only part most people read, so
 * a second audience addressed below the fold is a second audience not
 * addressed. The hero preview switches between a rental's job and a home's
 * instead, answering "is this for someone like me" with a picture.
 *
 * **Everything claimed here is something the product does today**, and
 * `tests/e2e/test_landing.py` is what keeps that true rather than aspirational.
 * It is why the feature strip below could not be written until the features
 * existed: no invented volume, no recurring schedules, and nothing described in
 * the present tense that is actually a plan.
 *
 * **Colour carries no meaning here except on the ladder.** `urgency.*` is the
 * one palette in this product that means something — the badges use it — so the
 * page borrows it only where it is drawing the ladder itself, and uses `brand`,
 * `accent` and `sun` everywhere else. A marketing page tinting a card "urgent"
 * because it looked good would teach people a colour the app then contradicts.
 */
function Step({ icon: Icon, title, tone, children }) {
  return (
    <li>
      <span
        className={`inline-flex h-12 w-12 items-center justify-center rounded-2xl ${tone}`}
      >
        <Icon />
      </span>
      <h3 className="mt-4 font-semibold">{title}</h3>
      <p className="mt-1 text-sm text-slate-600">{children}</p>
    </li>
  )
}

function TrustCard({ icon: Icon, title, children }) {
  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm transition hover:shadow-md">
      <span className="inline-flex h-10 w-10 items-center justify-center rounded-xl bg-brand-50 text-brand-600">
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
      {/* --- hero --- */}
      <section className="relative overflow-hidden bg-gradient-to-br from-brand-50 via-white to-accent-50">
        {/* Two soft washes rather than a flat panel. Pure CSS, no image asset
            — this repo ships no images on purpose. */}
        <div
          className="pointer-events-none absolute -right-24 -top-24 h-96 w-96 rounded-full bg-brand-200/40 blur-3xl"
          aria-hidden="true"
        />
        <div
          className="pointer-events-none absolute -bottom-32 -left-24 h-96 w-96 rounded-full bg-accent-200/40 blur-3xl"
          aria-hidden="true"
        />

        <div className="relative mx-auto max-w-6xl px-4 pb-16 pt-16">
          <div className="grid items-center gap-10 lg:grid-cols-2">
            <div>
              <p className="inline-flex items-center gap-2 rounded-full bg-white/80 px-3 py-1 text-sm font-semibold text-brand-700 shadow-sm ring-1 ring-brand-100">
                <span className="h-2 w-2 rounded-full bg-accent-500" aria-hidden="true" />
                {region ? `Now serving ${region}` : 'Vetted local cleaners'}
              </p>
              {/* The break is forced rather than left to the box width: at
                  desktop it wrapped as "The way / cleaning should / be.",
                  which splits the gradient phrase across two lines and reads
                  as a typo. On a phone it wraps here anyway. */}
              <h1 className="mt-5 text-4xl font-bold tracking-tight sm:text-5xl lg:text-6xl">
                The way cleaning{' '}
                <span className="block bg-gradient-to-r from-brand-600 to-accent-600 bg-clip-text text-transparent">
                  should be.
                </span>
              </h1>
              <p className="mt-5 max-w-lg text-lg text-slate-600">
                Somebody you&rsquo;d trust with your keys. Post the job, vetted
                local cleaners bid, you pick one — and pay when it&rsquo;s done.
              </p>

              <div className="mt-8 flex flex-wrap gap-3">
                <Link
                  to="/signup?role=owner"
                  className="btn-primary"
                  data-testid="owner-cta"
                >
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
        </div>
      </section>

      {/* --- how it works --- */}
      <section className="border-y border-slate-200 bg-white">
        <div className="mx-auto max-w-6xl px-4 py-16">
          <h2 className="text-2xl font-bold tracking-tight sm:text-3xl">
            How it works
          </h2>
          <ol className="mt-10 grid gap-8 sm:grid-cols-3">
            <Step
              icon={CalendarPlus}
              title="Post the job"
              tone="bg-brand-50 text-brand-600"
            >
              A turnover between guests, or a clean at home. You say when.
            </Step>
            <Step icon={PriceTag} title="Cleaners bid" tone="bg-sun-50 text-sun-600">
              They name their price. You see it next to their rating and their
              vetting.
            </Step>
            <Step
              icon={CardCheck}
              title="Pay when it’s done"
              tone="bg-accent-50 text-accent-600"
            >
              Charged when your cleaner marks the job complete. Never before.
            </Step>
          </ol>
        </div>
      </section>

      {/* --- what happens on the day. Both of these shipped before this
          section could mention them: the page may not describe a plan. --- */}
      <section className="bg-slate-900 text-white">
        <div className="mx-auto max-w-6xl px-4 py-16">
          <h2 className="text-2xl font-bold tracking-tight sm:text-3xl">
            On the day, you&rsquo;re not left wondering
          </h2>
          <p className="mt-3 max-w-2xl text-slate-300">
            The two questions everybody ends up phoning about, answered in the
            app instead.
          </p>

          <div className="mt-10 grid gap-6 md:grid-cols-2">
            <div
              className="rounded-2xl bg-white/5 p-6 ring-1 ring-white/10"
              data-testid="feature-on-the-way"
            >
              <span className="inline-flex h-11 w-11 items-center justify-center rounded-2xl bg-brand-500/20 text-brand-200">
                <Route />
              </span>
              <h3 className="mt-4 text-lg font-semibold">
                You know when they&rsquo;re on the way
              </h3>
              <p className="mt-2 text-slate-300">
                Your cleaner taps <em>on my way</em> when they set off, and again
                when they arrive. You&rsquo;re emailed the first one and can see
                the whole job — set off, on site, finished, and how long it took.
              </p>
              <p className="mt-3 inline-flex items-center gap-2 text-sm text-slate-400">
                <Clock className="h-4 w-4" />
                Times, not tracking — we don&rsquo;t follow anyone around.
              </p>
            </div>

            <div
              className="rounded-2xl bg-white/5 p-6 ring-1 ring-white/10"
              data-testid="feature-messages"
            >
              <span className="inline-flex h-11 w-11 items-center justify-center rounded-2xl bg-accent-500/20 text-accent-200">
                <Chat />
              </span>
              <h3 className="mt-4 text-lg font-semibold">
                Ask about the gate, not on the phone
              </h3>
              <p className="mt-2 text-slate-300">
                Once you&rsquo;ve booked each other, there&rsquo;s a message
                thread on the job. Both sides get emailed, so nothing waits for
                somebody to open the app.
              </p>
              <p className="mt-3 text-sm text-slate-400">
                Your phone number stays yours. Nobody swaps contact details to
                get an answer.
              </p>
            </div>
          </div>
        </div>
      </section>

      {/* --- the urgency ladder, the thing nobody else has --- */}
      <section className="mx-auto max-w-6xl px-4 py-16">
        <div className="grid gap-10 lg:grid-cols-[1fr_1.4fr] lg:items-center">
          <div>
            <h2 className="text-2xl font-bold tracking-tight sm:text-3xl">
              The tightest jobs rise to the top
            </h2>
            <p className="mt-3 text-slate-600">
              Every job is ranked by how little time is left on it. A cleaner
              opening the board sees the desperate ones first — which is why
              posting late still gets answered.
            </p>
          </div>
          <div className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm">
            <UrgencyLadder />
          </div>
        </div>
      </section>

      {/* --- trust --- */}
      <section className="border-t border-slate-200 bg-gradient-to-b from-white to-brand-50/50">
        <div className="mx-auto max-w-6xl px-4 py-16">
          <h2 className="text-2xl font-bold tracking-tight sm:text-3xl">
            Somebody is going into your place
          </h2>
          <p className="mt-2 text-slate-600">
            A rental or the house you live in — the line is the same.
          </p>

          <div className="mt-10 grid gap-6 sm:grid-cols-2 lg:grid-cols-4">
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
              <p className="text-sm font-semibold uppercase tracking-wide text-accent-200">
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
                <li>· Message the owner about the job, without giving out your number.</li>
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

      <section className="mx-auto max-w-6xl px-4 py-16 text-center">
        <h2 className="text-2xl font-bold tracking-tight sm:text-3xl">
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
