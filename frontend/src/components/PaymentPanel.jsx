import { useEffect, useState } from 'react'

import Alert from './Alert.jsx'
import { apiFetch } from '../lib/api.js'
import { formatCents } from '../lib/datetime.js'

/**
 * What the owner owes for a finished job, and the button that pays it.
 *
 * The button leaves this site: card details go to Stripe's own page and never
 * touch linx, the same reasoning as Stripe holding the cleaner's 1099 and the
 * background-check vendor holding their SSN.
 *
 * The panel deliberately shows the whole split — what the cleaner gets and what
 * the platform keeps — rather than one total. The cleaner named this price, the
 * fee comes out of it rather than being added on top, and an owner who cannot
 * see that has to take our word for where their money went.
 */
export default function PaymentPanel({ turnoverId, award, status }) {
  const [payment, setPayment] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const finished = Boolean(award?.completed_at) && status === 'completed'

  useEffect(() => {
    let cancelled = false
    apiFetch(`/turnovers/${turnoverId}/payment`)
      .then((data) => !cancelled && setPayment(data))
      .catch((err) => !cancelled && setError(err.message))
    return () => {
      cancelled = true
    }
  }, [turnoverId])

  async function pay() {
    setBusy(true)
    setError('')
    try {
      const { checkout_url: url } = await apiFetch(`/turnovers/${turnoverId}/pay`, {
        method: 'POST',
      })
      window.location.href = url
    } catch (err) {
      setError(err.message)
      setBusy(false)
    }
  }

  if (!finished && !payment?.status) return null

  const paid = payment?.status === 'succeeded'
  const refunded = payment?.status === 'refunded'
  const pending = payment?.status === 'processing'
  const stuck = payment?.status === 'requires_review'

  return (
    <div className="mt-6 card" data-testid="payment-panel">
      <h2 className="font-semibold" data-testid="payment-heading">
        {refunded
          ? 'Refunded'
          : paid
            ? 'Paid'
            : stuck
              ? 'Payment needs checking'
              : 'Ready to pay'}
      </h2>

      {payment?.amount_cents != null ? (
        <dl className="mt-3 grid gap-3 sm:grid-cols-3">
          <div>
            <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">
              Total
            </dt>
            <dd className="mt-1 text-sm font-medium" data-testid="payment-total">
              {formatCents(payment.amount_cents)}
            </dd>
          </div>
          <div>
            <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">
              To your cleaner
            </dt>
            <dd className="mt-1 text-sm" data-testid="payment-cleaner">
              {formatCents(payment.cleaner_cents)}
            </dd>
          </div>
          <div>
            <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">
              Platform fee
            </dt>
            <dd className="mt-1 text-sm">{formatCents(payment.platform_fee_cents)}</dd>
          </div>
        </dl>
      ) : (
        <p className="mt-2 text-sm text-slate-700">
          {award?.cleaner_name} marked this job done. Paying settles it for both of
          you — your cleaner is paid out of the same charge.
        </p>
      )}

      {stuck && (
        <p className="mt-3 text-sm text-amber-800" data-testid="payment-stuck">
          An earlier attempt didn&rsquo;t come back with an answer, so we&rsquo;ve left
          it alone rather than risk charging you twice. Someone is looking at it.
        </p>
      )}

      {refunded && payment?.failure_message && (
        <p className="mt-3 text-sm text-slate-700">{payment.failure_message}</p>
      )}

      {!paid && !refunded && !stuck && (
        <>
          <button
            type="button"
            className="btn-primary mt-4"
            onClick={pay}
            disabled={busy}
            data-testid="pay-button"
          >
            {busy ? 'Opening Stripe…' : pending ? 'Finish paying' : 'Pay now'}
          </button>
          <p className="mt-2 text-xs text-slate-500">
            You&rsquo;ll finish on Stripe&rsquo;s payment page. Your card details never
            reach linx.
          </p>
        </>
      )}

      {paid && (
        <p className="mt-3 text-sm text-slate-700">
          Thanks — your cleaner&rsquo;s share is on its way to them.
        </p>
      )}

      {error && (
        <div className="mt-4">
          <Alert>{error}</Alert>
        </div>
      )}
    </div>
  )
}
