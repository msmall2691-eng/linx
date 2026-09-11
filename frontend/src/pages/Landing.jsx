import { Link } from 'react-router-dom'
import { useEffect, useState } from 'react'

import { apiFetch } from '../lib/api.js'

export default function Landing() {
  const [region, setRegion] = useState(null)

  useEffect(() => {
    apiFetch('/config', { auth: false })
      .then((config) => setRegion(config.region_name))
      .catch(() => setRegion(null))
  }, [])

  return (
    <div className="mx-auto max-w-3xl px-4 py-16">
      <p className="text-sm font-semibold uppercase tracking-wide text-brand-600">
        {region ? `Now serving ${region}` : 'Short-term rental turnovers'}
      </p>
      <h1 className="mt-3 text-4xl font-bold tracking-tight sm:text-5xl">
        Turnover cleanings, bid and awarded.
      </h1>
      <p className="mt-4 text-lg text-slate-600">
        Owners post a turnover between checkout and the next checkin. Vetted local
        cleaners name their price. You pick one.
      </p>

      <div className="mt-8 flex flex-wrap gap-3">
        <Link to="/signup?role=owner" className="btn-primary">
          I own a rental
        </Link>
        <Link to="/signup?role=cleaner" className="btn-secondary">
          I clean
        </Link>
      </div>

      <div className="mt-16 grid gap-6 sm:grid-cols-2">
        <div className="card">
          <h2 className="font-semibold">For owners</h2>
          <p className="mt-2 text-sm text-slate-600">
            Post the checkout and the next checkin. The closer together they are,
            the higher the job sits on every cleaner&apos;s board.
          </p>
        </div>
        <div className="card">
          <h2 className="font-semibold">For cleaners</h2>
          <p className="mt-2 text-sm text-slate-600">
            Every cleaner passes ID verification and a background check before
            bidding. A person reviews each one — it takes a day or two.
          </p>
        </div>
      </div>
    </div>
  )
}
