---
name: marketplace-money-invariants
description: The authority contract for awarding work and moving money in linx. Load BEFORE writing or changing any code that touches Bid, Award, PaymentIn, Payout, agreed_price_cents, idempotency keys, or any call to Stripe — including refunds, transfers, destination charges, application fees, and webhook handlers. Also trigger on "accept a bid", "award the turnover", "charge the owner", "pay the cleaner", "refund", "double charge", "double booked", "race condition", "row lock", "FOR UPDATE", "idempotency", "Connect", "payment intent", "platform fee", "reconciliation". This skill CONSTRAINS other skills — when speed, simplicity, or any other guidance conflicts with it, this skill wins.
---

# Money and awarding invariants

Two operations in this product are irreversible from the user's side: promising
one cleaning to one cleaner, and moving money. Everything here exists because
both fail *silently* when they fail — an owner does not find out they promised
one job to two people until two vans arrive, and nobody finds out about a double
charge until a customer says so.

**This skill wins every conflict.** If a change is under deadline pressure and
something has to be cut, it is not the lock and it is not the idempotency key.
There is no "we'll tighten it after launch" version of these rules, because the
failure they prevent is the kind you discover from a customer rather than a test.

---

## 1. Awarding work — lock, then check

Any action that finalizes an `Award` — or any future action that hands one
indivisible thing to exactly one party — follows this order, with no steps
between:

1. `SELECT ... FOR UPDATE` on the `Turnover` row, **before anything is read or
   decided about it**, including who owns it.
2. Inside that lock: verify it is not already awarded, verify the bid, verify
   the cleaner is still cleared.
3. Still inside it: create the `Award`, mark the `Turnover` awarded, decline the
   other bids.
4. One commit, at the end. The commit is what releases the lock.

```python
# app/services/awards.py — the shape to copy
turnover = db.execute(
    select(Turnover).where(Turnover.id == turnover_id).with_for_update()
).scalar_one_or_none()
# ...every check below here, then one commit
```

**Checking first and locking later is the bug**, not a style difference: two
requests both read "not awarded" before either writes, and both proceed.

Three corollaries that are easy to get wrong:

- **No commit in the middle.** The session runs `expire_on_commit=False`, so an
  object read before a commit keeps its old values afterwards. A check made
  against a pre-commit copy is a check against the past. If a future change
  genuinely must commit partway (to fire a side effect before finalizing),
  **re-read the row fresh under the lock afterwards** — never carry the
  in-memory copy across the boundary.
- **No eager loading on the locking query.** `FOR UPDATE` cannot be applied
  across the outer join a `joinedload` adds. Lock the bare row; read the rest
  inside the lock.
- **The database constraint is a backstop, not the mechanism.** The partial
  unique index `uq_awards_live_turnover` (`turnover_id WHERE cancelled_at IS
  NULL`) turns a missed lock into a loud error instead of a double booking —
  which is the right failure, and still a bug. A route that *relies* on catching
  the integrity error has already lost the race it was supposed to prevent.

**The test that proves it must keep passing.** `backend/tests/test_awards.py`
holds two, and both are load-bearing:

- `TestTwoAcceptsAtOnce` — two accepts on two bids for one turnover, from two
  threads on two connections; exactly one 200, one 409, one live award.
- `TestTheLockIsReal` — proves the check happens *inside* the lock rather than
  before it, by changing the answer while the request waits on a lock another
  connection holds.

The second one exists because the first can pass against a broken
implementation on a run where the threads simply do not interleave. Keep both.
When you change this path, verify the way it was verified originally: delete
`with_for_update()`, watch both tests fail, put it back, watch both pass.

---

## 2. Stripe — idempotency is not optional (phase 6)

Nothing in this repo calls Stripe yet. The rules are written now because the
first call is exactly when they get skipped.

- **The key is derived, never generated.** `f"charge:award:{award.id}"`, built
  from an id that is already in the database. **Never `uuid4()` per attempt** —
  a genuine retry has to produce the *same* key to be recognized as a retry
  rather than a second charge. A fresh key per attempt does not "add safety"; it
  removes the only mechanism there was, and bills the customer twice.
- **Write the attempt before the call.** Set `attempted_at` (and the status) on
  the row and **commit**, then make the network call. A process that dies
  mid-call leaves a row visibly flagged for review (`requires_review`) rather
  than one that looks untouched. An unknown outcome is never assumed successful
  and never assumed failed.
- **One helper, one door.** Every Stripe call goes through the shared idempotent
  helper. No route, task, or webhook handler calls the Stripe SDK directly — the
  moment there are two doors, one of them is missing a key.
- **Destination charges, not two transactions.** One `PaymentIntent` with
  `transfer_data` pointing at the cleaner's connected account and
  `application_fee_amount` as the platform cut. Never build "collect from owner"
  and "pay cleaner" as two ledger entries reconciled later; the gap between them
  is where drift and double-payments live.
- **Express connected accounts, not Custom.** Identity verification and 1099
  reporting belong to Stripe.
- **Refunds are their own path, designed and tested before launch.** Refunding a
  destination charge must decide explicitly what happens to the
  `application_fee_amount` *and* to funds already transferred to the cleaner.
  Never write a refund as a simple reversal. Nothing may be left stranded: after
  a refund, collected still equals payout plus platform fee, or the path is
  wrong.
- **Integer cents everywhere. No floats, ever.**
- **Test mode until the go-live gate.** Going live means a new, separate Connect
  platform account under the new entity — never migrating an existing one, and
  never real pilot transactions under a personal SSN or another company's EIN.

---

## 3. When you touch this path

- Say in the PR body which invariant the change touches and how you verified it
  — not "added a lock" but "removed the lock, watched the race test fail, put it
  back".
- A money or locking change ships with its test in the same commit. The
  carry-over test list in `CLAUDE.md` is the checklist; a row on it that has no
  test yet is work, not a note.
- Run `money-path-reviewer` (`.claude/agents/money-path-reviewer.md`) on the
  diff before asking for review. A rule nobody checks is a rule that gets missed
  the week somebody is in a hurry.
