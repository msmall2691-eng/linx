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
| Payment receipt | owner |
| Payout notice | cleaner |
| Review received, once visible | both directions |

**A state transition is not done until its row here has both a sender and a test
asserting it fires.** Not "the code path exists" — a test that would fail if the
send were deleted. Deleting a send is silent in every other way; that test is
the only thing that makes it loud.

## Where this stands today

Phase 4 built the cancellation half early, because "never silently" belongs to
the no-show policy rather than to the notification feature:

- `app/services/alerts.py` names the event, resolves **every** recipient (admin
  is a role, not an address — a hardcoded address stops alerting the day someone
  new takes over the inbox), and records the alert.
- `app/services/awards.py` calls it on every cancellation of a live award —
  cleaner backing out, owner calling it off, no-show — never conditional on the
  cancellation being late.
- `backend/tests/test_cancellation.py` asserts the owner, the cleaner and an
  admin are all in the recipients.

It is deliberately **not** a notification system: no templates, no queue, no
`notifications` table, no delivery. Phase 5 replaces the sink (`_deliver`) and
fills in the rest of the table above.

## Rules for phase 5, decided now

- **One place decides recipients.** Call sites say what happened; the service
  works out who hears it.
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
