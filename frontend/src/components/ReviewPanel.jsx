import { useEffect, useState } from 'react'

import Alert from './Alert.jsx'
import { apiFetch } from '../lib/api.js'

/**
 * Leaving a review, and reading the ones that are visible.
 *
 * Used by both sides — an owner and a cleaner have identical rights here, which
 * is what "mutual" means. The same component renders on the owner's turnover
 * page and the cleaner's job card.
 *
 * **This screen deliberately cannot tell you whether the other side has
 * written.** The server does not send that, and nothing here infers it: no
 * "waiting on them", no count, no spinner that resolves differently. Knowing
 * they have written is knowing to hurry; knowing they have not is knowing you
 * can safely go first with a bad one. Both are exactly what the delay exists to
 * withhold.
 *
 * What it does say, once you have written, is that yours is being held — which
 * is true of every review and gives nothing away about theirs.
 */
function Stars({ value, onChange, name }) {
  return (
    <div className="flex gap-1" role="radiogroup" aria-label="Rating">
      {[1, 2, 3, 4, 5].map((star) => (
        <button
          key={star}
          type="button"
          role="radio"
          aria-checked={value === star}
          aria-label={`${star} out of 5`}
          onClick={() => onChange(star)}
          data-testid={`${name}-star-${star}`}
          className={`text-2xl leading-none transition ${
            star <= value ? 'text-amber-500' : 'text-slate-300 hover:text-amber-300'
          }`}
        >
          ★
        </button>
      ))}
    </div>
  )
}

function ReviewCard({ review, label }) {
  return (
    <div className="rounded-lg bg-slate-50 p-3" data-testid="review">
      <div className="flex items-center justify-between gap-3">
        <span className="text-xs font-medium uppercase tracking-wide text-slate-500">
          {label}
        </span>
        <span className="text-amber-500" aria-label={`${review.rating} out of 5`}>
          {'★'.repeat(review.rating)}
          <span className="text-slate-300">{'★'.repeat(5 - review.rating)}</span>
        </span>
      </div>
      {review.text && (
        <p className="mt-2 whitespace-pre-wrap text-sm text-slate-700">{review.text}</p>
      )}
    </div>
  )
}

export default function ReviewPanel({ turnoverId, side }) {
  const [state, setState] = useState(null)
  const [rating, setRating] = useState(0)
  const [text, setText] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let cancelled = false
    apiFetch(`/turnovers/${turnoverId}/reviews`)
      .then((data) => !cancelled && setState(data))
      .catch((err) => !cancelled && setError(err.message))
    return () => {
      cancelled = true
    }
  }, [turnoverId])

  async function submit(event) {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      // The action answers with the same shape the GET did, so replacing state
      // with it is safe — and if this was the second review, both are in it.
      setState(
        await apiFetch(`/turnovers/${turnoverId}/reviews`, {
          method: 'POST',
          body: { rating, text: text.trim() || null },
        }),
      )
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  // Nothing to review, and nothing written — no reason to take up the screen.
  if (!state) return null
  if (!state.can_review && !state.mine && state.visible.length === 0) return null

  const theirLabel = side === 'owner' ? 'Your cleaner' : 'The owner'
  const heldBack = state.mine && !state.mine.visible_at

  return (
    <div className="mt-6 card" data-testid="review-panel">
      <h2 className="font-semibold">Reviews</h2>

      {state.can_review ? (
        <form onSubmit={submit} className="mt-3 space-y-3">
          <p className="text-sm text-slate-600">
            Reviews stay hidden until you have both written, or two weeks pass. Neither
            of you sees the other&rsquo;s first, so there is nothing to react to.
          </p>
          <Stars value={rating} onChange={setRating} name="review" />
          <textarea
            rows={3}
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="How did it go?"
            className="field-input"
            aria-label="Your review"
            data-testid="review-text"
          />
          <button
            type="submit"
            className="btn-primary"
            disabled={busy || rating === 0}
            data-testid="submit-review"
          >
            {busy ? 'Saving…' : 'Leave review'}
          </button>
          <p className="text-xs text-slate-500">
            You can&rsquo;t change it afterwards — that&rsquo;s what keeps it honest
            for both of you.
          </p>
        </form>
      ) : (
        state.blocker &&
        !state.mine && (
          <p className="mt-2 text-sm text-slate-600" data-testid="review-blocker">
            {state.blocker}
          </p>
        )
      )}

      {state.mine && (
        <div className="mt-4 space-y-3">
          <ReviewCard review={state.mine} label="You wrote" />
          {heldBack && (
            <p className="text-xs text-slate-500" data-testid="review-held">
              Held until you have both written, or two weeks pass.
            </p>
          )}
        </div>
      )}

      {state.visible.length > 0 && (
        <div className="mt-3 space-y-3">
          {state.visible.map((review) => (
            <ReviewCard key={review.id} review={review} label={theirLabel} />
          ))}
        </div>
      )}

      {error && (
        <div className="mt-4">
          <Alert>{error}</Alert>
        </div>
      )}
    </div>
  )
}
