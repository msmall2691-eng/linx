import { useEffect, useState } from 'react'

import Alert from './Alert.jsx'
import { apiFetch } from '../lib/api.js'

/**
 * Whether money can reach this cleaner, and the way to fix it if it can't.
 *
 * Deliberately a separate panel from VettingPanel, sitting below it rather than
 * folded into it. They answer different questions — "may this person be in a
 * stranger's house" and "can a transfer land" — and the server keeps them
 * separate for the same reason: a Stripe verification delay must not read as a
 * vetting problem, and being cleared to bid must never imply being set up to be
 * paid.
 *
 * The blocker sentence is rendered verbatim, the same rule the vetting panel
 * follows. Nothing here re-derives why somebody cannot be paid.
 */
export default function PayoutPanel() {
  const [status, setStatus] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let cancelled = false
    // Ask Stripe rather than reading our copy: the cleaner may have just come
    // back from onboarding in another tab, and a stale "not set up" would send
    // them round the loop again.
    apiFetch('/payouts/status?refresh=true')
      .then((data) => {
        if (!cancelled) setStatus(data)
      })
      .catch((err) => {
        if (!cancelled) setError(err.message)
      })
    return () => {
      cancelled = true
    }
  }, [])

  async function startOnboarding() {
    setBusy(true)
    setError('')
    try {
      const { url } = await apiFetch('/payouts/onboarding', { method: 'POST' })
      window.location.href = url
    } catch (err) {
      setError(err.message)
      setBusy(false)
    }
  }

  if (!status) {
    return (
      <div className="card">
        <h2 className="font-semibold">Getting paid</h2>
        <p className="mt-1 text-sm text-slate-600">
          {error || 'Checking your payout setup…'}
        </p>
      </div>
    )
  }

  // A deployment with no Stripe key says so rather than showing a button that
  // cannot work. The payment path is off, not pretending.
  if (!status.payments_configured) {
    return (
      <div className="card" data-testid="payout-panel">
        <h2 className="font-semibold">Getting paid</h2>
        <p className="mt-1 text-sm text-slate-600">
          Payments aren&apos;t switched on for this site yet. Nothing to do — you&apos;ll
          be asked to set up payouts when they are.
        </p>
      </div>
    )
  }

  const ready = status.payouts_enabled

  return (
    <div className="card" data-testid="payout-panel">
      <div className="flex items-start gap-3">
        <span
          aria-hidden="true"
          className={`mt-1 h-2.5 w-2.5 shrink-0 rounded-full ${
            ready ? 'bg-emerald-500' : 'bg-amber-500'
          }`}
        />
        <div className="min-w-0 flex-1">
          <h2 className="font-semibold" data-testid="payout-heading">
            {ready ? 'Set up to get paid' : 'Not set up to get paid yet'}
          </h2>
          <p className="mt-1 text-sm text-slate-600" data-testid="payout-blocker">
            {status.blocker ??
              'Your payouts go straight to your Stripe account after each job you finish.'}
          </p>

          {!ready && (
            <>
              <p className="mt-3 text-sm text-slate-600">
                Stripe handles your identity check and your 1099 — we never see your
                bank details or your SSN.
              </p>
              <button
                type="button"
                className="btn-primary mt-4"
                onClick={startOnboarding}
                disabled={busy}
                data-testid="payout-setup"
              >
                {busy
                  ? 'Opening Stripe…'
                  : status.connected
                    ? 'Finish payout setup'
                    : 'Set up payouts'}
              </button>
              <p className="mt-2 text-xs text-slate-500">
                You can still bid and take jobs before this is done — you just
                can&apos;t be paid for them until it is.
              </p>
            </>
          )}
        </div>
      </div>

      {error && (
        <div className="mt-4">
          <Alert>{error}</Alert>
        </div>
      )}
    </div>
  )
}
