---
name: notification-completeness
description: The fixed list of events linx must tell people about, and the rule that a state transition is not finished until its notification has both a sender and a test. Load BEFORE adding or changing any turnover, bid, award, or payment state transition — publishing, bidding, accepting, declining, cancelling, no-shows, re-posts, charges, payouts, reviews — and before building phase 5. Also trigger on "notify", "notification", "email", "SMS", "alert", "did anyone tell the owner", "the cleaner didn't know", "silently", "should we send".
---

# Nobody finds out by showing up

Missing and duplicated notifications cost more than missing features here. A
marketplace where an owner learns their cleaner cancelled by arriving at a dirty
house does not get a second chance, and a system that sends the same alert three
times gets muted, which is the same thing as sending none.

So the list is **fixed before the feature is built**, and the completeness rule
is structural rather than a matter of remembering.

## The event list

| Event | Goes to |
|---|---|
| New turnover posted within a cleaner's service radius | matching cleaners |
| Bid received | owner |
| Bid accepted | the winning cleaner |
| Bid declined | each declined cleaner |
| Turnover reminder, day-of | both sides |
| Cleaner cancels close to checkout → urgent re-post | owner **and** admin |
| Turnover unclaimed past the cutoff | owner **and** admin |
| Job marked complete by the cleaner | owner |
| Payment receipt | owner |
| Payout notice | cleaner |
| Review received, once visible | both directions |

**A state transition is not done until its row here has both a sender and a test
asserting it fires.** Not "the code path exists" — a test that would fail if the
send were deleted. Deleting a send is silent in every other way; that test is
the only thing that makes it loud.

## Where this stands today

Twelve of the thirteen events have a sender and a test. Only `review_received`
is still declared without one; reviews land in phase 7, and
`tests/test_notifications.py` asserts that it is declared-and-unwired so the
phase inherits a list rather than somebody's memory.

**The list grew by one in phase 6, and that is how this is supposed to work.**
`job_completed` was added with the transition it belongs to: once the owner is
charged for a finished job rather than a booked one, "the cleaner says it is
done" stopped being nobody's business and became the moment the owner has to
act. Nobody hearing it means nobody pays and the cleaner is never paid. Adding
it took a migration *and* broke the closed-list test — which is the point. The
list being closed is what turns a new event into a decision somebody makes out
loud instead of a string that appears in one call site.

The mechanics underneath, each load-bearing:

- **`queue()` does not commit.** Rows join the caller's open transaction, so a
  notification cannot survive a rollback of the thing it describes.
  `deliver_pending()` runs after the commit.
- **`dedupe_key` is a unique constraint** built from the event plus a stable
  database id — never a clock, never a random value. It is why the scheduled job
  can run every five minutes and send one reminder.
- **A row is `pending` until a sender says otherwise.** With no `SMTP_HOST` the
  logging sender reports `delivers=False`, so rows stay pending — the log is not
  a delivery.
- **A drain claims its rows before sending them** — one `UPDATE ... FOR UPDATE
  SKIP LOCKED`, committed before the network call. The dedupe key makes the row
  unique per transition; it does nothing about two overlapping drains both
  sending it.
- **Money notifications are queued from the webhook**, never from the request
  that starts a checkout. A receipt for a payment that then failed is worse than
  no receipt, because it is believed.

## The rules these were built to, still binding

- **One place decides recipients.** Call sites say what happened; the service
  works out who hears it. `admin` is a role, not an address.
- **Duplicates are as bad as misses.** An event fires once per transition. If a
  retry could fire it twice, it needs a key — the same reasoning as guardrail 2,
  for the same reason.
- **Record before you send.** Write the notification row in the same transaction
  as the state change; deliver after. An alert emitted before the commit can
  describe something that then rolled back; an alert with no record at all
  cannot be audited when someone says "nobody told me".
- **A failed delivery is visible, never swallowed.** Unknown outcome is not
  success.
- **In-app messaging stays out of scope for v1.** Email and SMS are enough at
  this size; the fixed list above is the whole surface.

## When you add a transition

Ask, in the PR body: who learns about this, and what breaks if they don't? If
the answer is "nobody needs to know", say that explicitly — that is a decision,
and it should be visible as one.
