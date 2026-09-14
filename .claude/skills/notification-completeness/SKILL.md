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

Phase 5 built it. `app/services/notifications.py` is the only place that decides
who hears about anything, and nine of the twelve events in `NotificationEvent`
have a sender and a test. The three that do not — payment receipt, payout notice,
review received — have no state transition to fire them until phases 6 and 7, and
`tests/test_notifications.py` asserts exactly that: declared, unwired, and the
list closed at twelve. That test is what makes "we forgot one" loud.

The mechanics, each one load-bearing:

- **`queue()` does not commit.** It writes rows into the caller's open
  transaction, so a notification cannot survive a rollback of the thing it
  describes. `deliver_pending()` is called after the commit.
- **`dedupe_key` is a unique constraint** built from the event plus a stable
  database id — a turnover, a bid, a recipient. Never a clock, never a random
  value. It is why `python -m app.tasks.scheduled` can run every five minutes and
  send one reminder rather than twelve, and why a retried request cannot double
  up. A duplicate insert is caught on a savepoint so it cannot poison the
  caller's transaction.
- **A row is `pending` until a sender says otherwise.** Failures write `failed`
  with the reason. With no `SMTP_HOST` the logging sender runs and reports
  `delivers=False`, so rows stay `pending` — the log is not a delivery.
- **The outbox is drained twice over**: by the request that queued the rows, and
  by the scheduled pass, so a process that died between commit and send does not
  lose the notification.
- **A drain claims its rows before sending them** — one `UPDATE ... FOR UPDATE
  SKIP LOCKED`, committed before the network call. The dedupe key makes the row
  unique per transition; it does nothing about two overlapping drains both
  sending it, and since every request drains, overlapping is the ordinary case.
  A row attempted with no outcome is not retried: assuming failure is how
  somebody gets the same message twice.

`app/services/alerts.py` is gone. Its two callers in `app/services/awards.py` now
call `notifications.award_cancelled`, and `tests/test_cancellation.py` asserts on
notification rows rather than log records — still checking that the owner, the
cleaner and an admin are all told on every cancellation of a live award, never
conditional on it being late.

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
