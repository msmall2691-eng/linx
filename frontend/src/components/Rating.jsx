/**
 * Somebody's rating, as a number rather than a ranking.
 *
 * Shown wherever a person is being judged — an owner reading the bids on their
 * job, a cleaner looking at their own profile — and computed in exactly one
 * place on the server (`app/services/reviews.py`). The average arrives already
 * rounded, deliberately: a screen that rounds it itself is a second author of
 * the number, and two screens then disagree about whether somebody is a 4.4 or
 * a 4.5.
 *
 * **Nothing here sorts.** Rating-weighted ranking is out of scope for v1: the
 * bid list stays cheapest-first and the bench board stays urgency-first, so a
 * cleaner with no reviews yet is not buried in a marketplace short of supply.
 * "No reviews yet" is therefore a neutral statement of fact, not a warning.
 */
export default function Rating({ reputation, testId = 'rating' }) {
  if (!reputation || reputation.count === 0) {
    return (
      <span className="text-xs text-slate-500" data-testid={testId}>
        No reviews yet
      </span>
    )
  }

  return (
    <span
      className="text-xs text-slate-600"
      data-testid={testId}
      aria-label={`${reputation.average} out of 5 from ${reputation.count} ${
        reputation.count === 1 ? 'review' : 'reviews'
      }`}
    >
      <span className="text-amber-500" aria-hidden="true">
        ★
      </span>{' '}
      <strong data-testid={`${testId}-average`}>{reputation.average.toFixed(1)}</strong>
      {' · '}
      <span data-testid={`${testId}-count`}>
        {reputation.count} {reputation.count === 1 ? 'review' : 'reviews'}
      </span>
    </span>
  )
}
