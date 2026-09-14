# linx — engineering guide

A two-sided marketplace connecting short-term-rental (STR) property owners with
independent cleaners for turnover cleanings. One category, one region at launch,
a standing bid-and-award relationship rather than one-off lead sales.

**This repository is fully standalone.** It does not import from, read from, or
assume the existence of any other project. If another codebase is visible in
your environment, ignore it entirely — nothing here may depend on it. That
isolation is what makes the liability separation real, not just the branding.

---

## The three guardrails

These are not style preferences. Each one exists because the failure it prevents
is silent, expensive, and discovered in production. Read them before writing
feature code, and re-read #3 before deleting anything.

### Guardrail 1 — Concurrency on any action that awards work

Any action that awards work to exactly one party must hold a database row lock
across the whole check-then-write, not just the write.

When an owner accepts a bid:

1. Open a transaction.
2. Take `SELECT ... FOR UPDATE` on the `Turnover` row **before checking anything
   about it**. In SQLAlchemy 2.0: `session.execute(select(Turnover).where(Turnover.id == turnover_id).with_for_update())`.
3. Inside that lock, verify the turnover is not already awarded. If it is,
   reject the accept.
4. If it is not, create the `Award` row and mark the `Turnover` awarded.
5. Commit — all of it in the same transaction.

Because the row is locked for the entire check-then-write, two near-simultaneous
accepts on two different bids for the same turnover cannot both succeed. The
second one to acquire the lock sees the turnover already awarded and is
rejected. Checking first and locking later is the bug: both requests read
"not awarded" before either writes.

If any step must commit partway through (for example, to trigger a side effect
before finalizing), **re-read the row fresh under the lock afterward**. Never
trust an in-memory copy across a commit boundary — it is stale by definition.

There is a test for this: two simultaneous accepts on different bids for the
same turnover, asserting exactly one wins. It must keep passing.

### Guardrail 2 — Idempotency on any Stripe call that could be retried

Every Stripe call that could plausibly be retried — charging a `PaymentIntent`,
creating a `Transfer`, issuing a refund — must:

1. **Pass an `idempotency_key` derived from a stable identifier already in the
   database** — the `Award` id or `Turnover` id, plus a constant naming the
   operation (e.g. `f"charge:award:{award.id}"`). **Never a freshly generated
   value.** A genuine retry has to produce the *same* key to be recognized as a
   retry rather than a new charge. A `uuid4()` per attempt defeats the entire
   mechanism and bills the customer twice.

2. **Write the attempted state to the row before making the network call** —
   e.g. set `payment_attempted_at` and commit, *then* call Stripe. If the
   process crashes mid-call, that row is left visibly flagged for manual review
   instead of looking untouched. An unknown outcome must never be assumed
   successful or assumed failed.

Related money rules, for when payments land (phase 6):

- Use a **Stripe Connect destination charge**, not two independent transactions.
  One `PaymentIntent` with `transfer_data` pointing at the cleaner's connected
  account and `application_fee_amount` as the platform cut. Stripe settles the
  split atomically. Never build "collect from owner" and "pay cleaner" as two
  separate ledger entries reconciled later — the gap between them is exactly
  where double-payments and silent drift live.
- Use **Express** connected accounts, not Custom. Express puts identity
  verification and 1099 reporting on Stripe. Custom puts them on us.
- **Refunds get their own deliberately designed path**, written and tested
  before launch. Refunding a destination charge must decide what happens to the
  `application_fee_amount` and to already-transferred cleaner funds; it is not a
  simple reversal.
- All money is stored as **integer cents**. No floats, anywhere, ever.

### Guardrail 3 — Before removing a step from any existing flow, find out what depends on it

This is a process rule, not a code pattern. It matters as much as the other two.

If a change deletes or bypasses something — an approval step, a notification, a
status transition, a permission check, a validation — **search the codebase
first for what else assumes that step happens**, and note what you found in the
change description (commit message or PR body).

Concretely, before the change ships:

- Grep for the function, field, status value, or event name being removed.
- Check for code that reads the state the removed step used to write.
- Check for tests that assert the step happened.
- Write down, in the PR, what depends on it and why removing it is still safe.

The failure this prevents is silent: something downstream kept working by
accident until it didn't, and nothing failed loudly at the moment of the change.

---

## The guardrails, as things that check themselves

A rule nobody checks is a rule that gets missed the week somebody is in a hurry,
so each guardrail above is paired with a skill Claude Code reads and an agent
that verifies the rule was actually followed:

| Guardrail | Skill — the rule | Agent — the check |
|---|---|---|
| 1 (locking) and 2 (idempotency) | `.claude/skills/marketplace-money-invariants/` | `.claude/agents/money-path-reviewer.md` |
| 3 (before removing a step) | `.claude/skills/seam-check-before-removal/` | `.claude/agents/seam-regression-auditor.md` |

Two more skills cover the rules that are not guardrails but fail the same way —
silently, and discovered by a person rather than a test:

- `.claude/skills/trust-gate-single-source/` — `can_take_jobs` has one author,
  two layers of enforcement, and no override.
- `.claude/skills/notification-completeness/` — the fixed event list, and the
  rule that a state transition is not done until its notification has both a
  sender and a test.

The agents are **read-only by design**: they report, they do not fix. A reviewer
that can quietly commit its own corrections turns a finding into a change nobody
read.

---

## Stack

| Layer | Choice |
|---|---|
| Backend | FastAPI, SQLAlchemy 2.0 (typed `Mapped[]` style), Alembic |
| Database | PostgreSQL (production **and** tests — see below) |
| Frontend | React 18, Vite, Tailwind CSS |
| Auth | JWT, role-gated (owner / cleaner / admin) |
| Deploy | Single container; the backend serves the built frontend. Railway, its own project, its own Postgres. |

**Tests run against real Postgres, not SQLite.** Guardrail 1 depends on
`SELECT ... FOR UPDATE`, which SQLite does not implement — a test suite on
SQLite would pass while the production race condition stayed wide open. See
`backend/tests/conftest.py`.

**There are browser tests, and they earn their keep.** `backend/tests/e2e/`
drives the real app in a real browser. They exist because one class of bug is
invisible to endpoint tests: an action that returns 200 while the screen it
belongs to goes blank. Exactly that shipped in the first pass at phase 2 — the
cancel endpoint answered with a smaller shape than the GET, the frontend
replaced its state with it, and the next render read a field off `undefined`.
Green suite, clean server log, dead page. They are opt-in locally (`LINX_E2E=1`,
a frontend build, playwright) and run as their own CI job.

**One shape per resource.** Every endpoint returning a single turnover returns
`TurnoverDetailOut`; only the list returns the lighter `TurnoverOut`. A detail
screen replaces its state with whatever an action answers, so an action that
returns less than the GET silently drops a field and blanks the page. Any new
resource with a detail screen follows the same rule.

---

## Domain model

This is a two-sided marketplace, not multi-tenant SaaS. Everyone shares one
marketplace; the boundary that matters is **role** (owner / cleaner / admin),
not tenant. There is deliberately no `org_id`-style scoping.

| Table | Purpose |
|---|---|
| `users` | One table for all three roles, role-gated |
| `cleaner_profiles` | Bio, service-area lat/lng + radius, vetting statuses, `can_take_jobs` |
| `properties` | Belongs to an owner |
| `turnovers` | A cleaning job: checkout/checkin times, status, `urgency` |
| `bids` | A cleaner names a price on a turnover |
| `awards` | One **live** row per turnover — guardrail 1 governs this; a cancelled one is kept as history |
| `documents` | Cleaner vetting uploads (id / insurance / reference), admin-reviewed |
| `reviews` | Mutual, delayed reveal |
| `payments_in` | Collects from the owner |
| `payouts` | Pays the cleaner |

Two fields carry more weight than they look like they do:

- **`cleaner_profiles.can_take_jobs`** is a single honest boolean, computed
  from the vetting statuses. A bidding gate and a display badge that can
  disagree with each other is a real trap — one says "verified" while the other
  silently blocks bids, and nobody can tell which is lying.

  It is enforced in two layers, and neither is optional:

  1. The column is a **Postgres generated column**. No application code can
     write it; `UPDATE ... SET can_take_jobs` errors. There is deliberately no
     admin endpoint to override it, because an override is precisely how a
     cleaner ends up able to bid without a finished check.
  2. **`app/services/vetting.py` turns that boolean into a reason**, and every
     surface that mentions clearance reads it: the bidding gate, the cleaner's
     own profile panel, and the admin queue. Never re-derive the rule inline —
     the frontend renders `vetting` verbatim for the same reason.

- **`turnovers.urgency`** is derived from how close checkout is to the next
  checkin (or to now, for a standing vacancy). The closer, the more urgent. This
  ladder is the product's core pricing and priority signal — it is not
  decoration.

  `app/services/urgency.py` is the only place that decides, and
  `app/services/turnovers.py` owns the only writes to the column. Two separate
  measures, because a turnover means different things depending on whether the
  next guest is booked: with a checkin, the window between the two *is* the job;
  without one, the signal is how soon checkout itself arrives. The rungs:

  | Rung | Condition |
  |---|---|
  | `same_day` | checkin lands on the same **region-local** calendar day as checkout |
  | `urgent` | window (or lead time) under 24h — including a checkout already past |
  | `soon` | under 72h |
  | `standard` | anything else |

  "Same day" is answered in `REGION_TIMEZONE`, never UTC: a 4pm checkout and a
  10pm checkin are one day in Portland and two in UTC. The API refuses naive
  timestamps rather than guessing, and the frontend converts a `datetime-local`
  input through the region's zone — an owner who lives in California must not
  post a Portland checkout three hours off.

  A turnover that a booking came undone on (`reopened_at`) is read twice: on
  its window, and on the time left until checkout — the same measure a standing
  vacancy uses, because once nobody is staffed for it, finding somebody is the
  clock that matters again. The more urgent of the two wins. That is an input to
  the one function, not a second author: nothing outside `urgency.py` decides a
  rung, and `reopened_at` can only raise one.

  A residential job has no checkin and so is always read on the second measure
  — time until the job — which is the right meaning rather than a workaround,
  and is why residential needed no second ladder. `same_day` is unreachable for
  one, because it describes a guest arriving the day another leaves.

  The "nobody has claimed this and checkout is tomorrow" alarm is deliberately
  **not** urgency. That is an operational alert with its own cutoff and its own
  recipients, and it belongs to the unclaimed-turnover path in phase 5.

### Two kinds of property

**Residential was out of scope and was deliberately reopened.** The original
list said "non-STR recurring residential cleaning", and the reasoning was real:
a short-term rental's clean is defined by the gap between one guest leaving and
the next arriving, and that window is what the whole urgency ladder measures. A
home has no such window.

What made it cheap rather than a rewrite is that the model already had the
shape. A turnover with `checkin_at IS NULL` is a standing vacancy, and its
urgency is already measured as *time until the job* rather than the length of a
window — which is exactly what a scheduled house clean is. So residential rides
the existing table and the existing ladder rather than forking either.

| | Short-term rental | Residential |
|---|---|---|
| `properties.property_type` | `short_term_rental` | `residential` |
| `turnovers.checkin_at` | the next guest, or null | **always null** |
| `turnovers.service_type` | `turnover` | `standard` / `deep` / `move_out` |
| Urgency read as | the window between guests | how soon the job is |
| `same_day` reachable | yes | **no** — there is no next guest |

Three rules hold it together:

- **`app/services/turnovers.py` decides what fits what**, in one place
  (`service_type_for`, `checkin_for`). The route asks; it does not carry a copy.
  A form that offers an option the API refuses is a form that lies.
- **A mismatch is refused, never corrected.** An owner who asked for a move-out
  clean and silently got a turnover finds out from the cleaner who turned up
  expecting two hours' work.
- **A checkin on a home is a category error**, not a slightly wrong value — it
  would put the job on a rung that measures something a home does not have. The
  API refuses it rather than dropping it, because an owner who typed a time into
  a field deserves to know it was ignored.

`properties.square_feet` is **optional on purpose**. Plenty of owners genuinely
do not know it, and a required field somebody has to guess at produces a number
worse than no number. When it is there it is the single most useful thing a
cleaner has for pricing, so it is asked for and shown on the board.

The privacy boundary below does not soften for a home — it matters more.
Somebody lives there.

---

## The privacy boundary

**A cleaner who has bid has not been hired.** The bench board therefore uses its
own response shapes (`app/schemas/board.py`) rather than filtering the owner's:

| Shown | Withheld until award |
|---|---|
| city, state, ZIP, beds/baths/sq ft | street address |
| property type and scope of work | whether anybody is home right now |
| cleaning notes, timing, urgency | **`access_notes`** — gate codes, lockbox locations |
| owner's budget, distance in miles | the owner's identity and contact details |
| the cleaner's **own** bid | any other cleaner's bid or price |

`BoardPropertyOut` is a separate model, not a subset of `PropertyOut`, so adding
a field to the owner's shape cannot quietly widen what cleaners see — a new
field has to be added to the board shape deliberately. There are tests that
assert the lockbox code and street address appear nowhere in the board
response, including in the rendered page.

**The line moves on award, and moves back when the award ends.** An awarded
cleaner reads their job through `/board/jobs`, whose `AwardedPropertyOut` is a
third model again rather than a widened board shape — the address and the access
notes are in it because somebody has to open the door. Access follows the *live*
award, decided in one place: cancel the booking and the access notes come back
empty, and the job's own detail endpoint answers 404. The owner's identity stays
withheld either way; phase 5's notifications are how the two sides reach each
other.

**Vetting documents are never on a public path.** Uploads get a generated key
(never a client-supplied filename, which is a path-traversal primitive), are
written outside anything the server exposes, and come back only through an
authenticated endpoint that checks who is asking. On Railway the storage
directory **must be a mounted volume** — a container filesystem is replaced on
every deploy, so without one the uploaded IDs vanish while their rows survive.

## Background checks

`app/services/background_check.py`. Three rules:

- **The vendor collects the sensitive data, not us.** Checkr's candidate +
  invitation flow has the cleaner give their SSN and date of birth to Checkr
  directly. Neither ever touches this database — the same reasoning as using
  Stripe Express for payouts.
- **A "consider" result is never an automatic rejection.** Checkr returns
  `clear` or `consider`; `consider` means something surfaced that a human must
  weigh, and acting adversely on it has a legally defined process (FCRA adverse
  action). It parks in `pending` with a note. Anything unrecognised also parks
  in `pending` — an unknown vendor state must never read as a clearance.
- **No key configured means manual, not broken.** Without `CHECKR_API_KEY` the
  manual provider is used and an admin records the outcome. That is the launch
  posture, and it means vetting works end to end before a Checkr account exists.

The Checkr HTTP client is **not verified against the live API.** It is written
to Checkr's documented interface with its response handling covered by tests
against a mocked transport; no request has ever been made to a real Checkr
account from this codebase. Walk one candidate through a sandbox key before
relying on it.

## Trust & safety rules that constrain the code

- **ID verification is manual at launch.** A human reviews an uploaded photo ID
  and one reference before a cleaner can bid. Do not automate this away at MVP.
  It is the safety valve between "hands off" and "anyone can walk into a
  stranger's house."
- **A real background check, not just an ID photo.** A photo ID confirms
  identity, not history. `background_check_status` gates bidding.
- **Insurance / COI is a flag, not a gate, at v1.** Requiring it up front while
  supply is scarce kills the launch. Show it prominently as missing instead.
- **Reviews are mutual and delayed.** Neither side's review is visible until
  both are submitted or a timeout passes. Show-immediately systems create an
  incentive to leave a pre-emptive bad review to suppress the other side's.
  Built in phase 7 (`app/services/reviews.py`):
  - **`visible_at` has exactly one author.** `reviews.reveal_pair` is the only
    function that writes it, and there is a test that greps for a second one —
    a rival writer would fail no other test until it disagreed in production.
  - **The response shape carries no tell.** Not just the text: the *fact* that
    the other side has written is itself the signal, because knowing they have
    is knowing to hurry and knowing they have not is knowing you can safely go
    first with a bad one. There is no "awaiting", no count, no timestamp to
    difference — and a browser test asserts one side's rendered panel is
    byte-identical before and after the other side writes.
  - **Silence is not a veto.** A one-sided review is revealed anyway after
    `REVIEW_REVEAL_AFTER_DAYS` (14), swept by `app/tasks/scheduled.py`. Without
    that sweep, refusing to answer becomes the way to bury a bad review — the
    same suppression, achieved by doing nothing. It is the one scheduled job
    that is load-bearing rather than a courtesy.
  - **No edits.** A review you can rewrite once the other side's appears is a
    review you can rewrite *in response to* it.
  - **Reviewable means finished.** A cancellation or a no-show is not: there is
    no mutual review to balance when one side did not turn up, and the product
    already records it as `was_no_show` plus an admin alert, answered by a human.
  - **`review_received` fires on reveal, never on write** — telling somebody a
    review has landed hands them what the delay withholds. The recipient is the
    person reviewed, not the author.
  - **Ratings are shown, never ranked.** `reviews.reputation_of` is the one
    definition — visible reviews only, so a rating cannot leak the hidden half
    by arithmetic — with `reputation_counts` its batch form for a list screen,
    and a test asserting the two agree. It reaches an owner on the bid list and
    a cleaner on their own profile, and nothing sorts by it (see the v1 scope
    list below). `GET /cleaners/{id}/reputation` is readable by any signed-in
    user because a rating is the thing a marketplace makes public; there is
    deliberately no public, unauthenticated cleaner directory at v1.
- **Disputes go to a human inbox, not a bot, at v1.**
- **No-show / cancellation policy is defined before launch.** Built in phase 4
  (`app/services/awards.py`), and the definition is:
  - **Any** cancellation of a live award — the cleaner backs out, or never turns
    up — re-posts the job to the bench and alerts the owner, the cleaner and an
    admin immediately. Never conditional, never silent.
  - **"Live" means `awarded` *or* `in_progress`**, named once in
    `awards.LIVE_BOOKING_STATUSES`. Phase 6 made `in_progress` reachable, and
    keying these paths on `awarded` alone would have meant a cleaner who tapped
    "I'm on site" could no longer back out, and — worse — could make a no-show
    unrecordable by tapping it from the driveway and leaving. Both refusals
    would have been a 409 reading "there is nobody booked", which is false, on
    the one path this policy says is never conditional. A `completed` job is
    deliberately outside that list: backing out of finished work is a dispute
    and a refund, not a cancellation.
  - A cancellation inside **48 hours** of checkout (`LATE_CANCELLATION_WITHIN`)
    is *late*, and the alert says so. It does not change what happens; it is the
    number the policy is written against, kept in one place so the policy and
    the alert cannot disagree.
  - A **no-show** is flagged separately on the award (`was_no_show`), because
    nobody gave notice and the response is not the same as a cancellation.
  - An owner cancelling on a booked cleaner is allowed but never quiet: a reason
    is required, and the cleaner and an admin are told.
  - The award row is **cancelled, not deleted** — who backed out is the history
    a dispute is argued from.

## Notification events — the fixed list, built in phase 5

Missing and duplicated notifications cost more than missing features. The list
was fixed before the feature was built, and it is closed — `NotificationEvent`
in `app/models/enums.py` holds exactly these and nothing else:

- New turnover posted within a cleaner's service radius
- Bid received (owner)
- Bid accepted (cleaner) / bid declined (cleaner)
- Turnover reminder, day-of, both sides
- Cleaner cancels close to checkout → urgent re-post + alert to owner and admin
- Turnover unclaimed past a defined cutoff → alert to owner and admin
- Job marked complete by the cleaner → owner (added in phase 6, see below)
- Payment receipt (owner) / payout notice (cleaner)
- Review received (both directions, once visible)

**All thirteen now have a sender.** Nothing is declared-and-unwired any more;
the test that guarded that has flipped to guarding the other direction — a new
enum value with nothing behind it fails, which is the conversation adding one is
supposed to start.

**The list grew by one, on purpose.** `job_completed` was added in phase 6
alongside the transition it belongs to. Before money hung off completion, "the
cleaner says it is done" was nobody's business; now an owner who is never told
is an owner who never pays, and a cleaner who did the work and hears nothing.
Adding it meant a migration and a failing test, which is exactly the
conversation a new event is supposed to start — the list being closed is what
makes opening it a decision.

`app/services/notifications.py` is the one place that decides any of this.
Phase 4's `app/services/alerts.py` is gone, replaced by it.

- **Call sites say what happened; the service decides who hears.** `admin` is
  resolved from the role, never an address — a hardcoded one stops alerting the
  day somebody new takes over the inbox. Matching cleaners are resolved from
  service radius **and** `can_take_jobs`, so a cleaner who cannot bid is not told
  about work they cannot take.
- **Recorded inside the transaction, delivered after it.** `queue()` writes rows
  into the caller's open transaction, so a notification can never describe a
  state change that then rolled back. `deliver_pending()` runs after the commit
  and is a plain outbox drain: a process that dies between the two leaves rows
  the next pass picks up, rather than a send nobody can account for.
- **`dedupe_key` is a unique constraint, not a convention.** Every key is built
  from the event plus a stable id already in the database — a turnover, a bid, a
  recipient — never a clock or a random value, for the same reason guardrail 2
  forbids a fresh idempotency key. "Fires once per transition" is a property of
  the database. A duplicate insert is caught on a savepoint, so it cannot poison
  the caller's transaction.
- **An unknown outcome is never success.** A row is `pending` until a sender
  reports it delivered; a failure writes `failed` with the reason and stays
  visible. Without `SMTP_HOST` the logging sender runs, and it reports
  `delivers=False` — the row stays `pending`, because nothing was sent.
- **The send is claimed before it is made.** The dedupe key makes the *row*
  unique per transition; it says nothing about two drains picking the same row
  up and both sending it, and every request drains now. `deliver_pending()`
  therefore claims its rows in one `UPDATE ... FOR UPDATE SKIP LOCKED` and
  commits that claim *before* the network call — guardrail 2's ordering applied
  to a send. A row attempted with no outcome is deliberately not retried: an
  unknown outcome may not be assumed failed any more than successful, and
  assuming failure is how somebody gets the same message twice.

Three things hang off the clock rather than off something a person did: the
day-of reminder, the unclaimed alarm, and — from phase 7 — the review reveal.
`app/tasks/scheduled.py` is their entry point (`python -m app.tasks.scheduled`),
runs safely as often as you like because of the dedupe key, and drains the
outbox on the way past. Their three windows (`REMINDER_HOURS_BEFORE`,
`UNCLAIMED_ALERT_HOURS_BEFORE`, `REVIEW_REVEAL_AFTER_DAYS`) are separate
settings on purpose — one is a courtesy, one is an operational alarm still
deliberately **not** a rung on the urgency ladder, and one is how long a review
waits for an answer that may never come. The reveal is the only one of the three
that is load-bearing rather than a courtesy: if the job stops running, silence
becomes a veto and refusing to answer becomes the way to bury a bad review.

---

## Explicitly out of scope for v1

Do not build toward these:

- **Recurring schedules.** Residential jobs are one-off at v1: a home can be
  posted as often as its owner likes, but nothing repeats on its own yet.
  Repeating means materialising future jobs, which needs its own rules about
  what happens when one is cancelled or the schedule changes — a decision, not
  a checkbox.
- Automated dispute resolution
- Rating-weighted search ranking — phase 7 shows a rating on the owner's bid list and on a cleaner's own profile, but nothing orders by it: the bid list stays cheapest-first and the bench board urgency-first, so a cleaner with no reviews is not buried in a marketplace short of supply
- Multi-region logic — one region is hardcoded
- Native mobile apps — responsive web only
- In-app messaging — email/SMS notifications are enough at this size

---

## Phased build plan

Each phase is reviewed before the next begins. Do not get ahead of the current
phase.

| Phase | Scope | Status |
|---|---|---|
| 1 | Scaffolding, JWT role auth, full schema migration | **done** |
| 2 | Owner side: properties & turnovers, with `urgency` | **done** |
| 3 | Cleaner side: profile, vetting docs, background check, admin review queue, bidding | **done** |
| 4 | Award + guardrail-1 concurrency + no-show / cancellation path | **done** |
| 5 | Notifications (the event list above) | **done** |
| 6 | Stripe Connect, test mode end to end, refunds, reconciliation | **done** |
| 7 | Mutual delayed-reveal reviews | **done** |
| 8 | Admin console — vetting queue, dispute inbox, unclaimed alerts, ledger | not started |
| 9 | Pilot launch checklist — new Connect platform account under the new entity | not started |

**Phase 1 built the schema for every table, with no business logic.** Tables for
later phases exist and are empty on purpose. The shape is settled now; the
behavior arrives in its own phase.

## Stripe

All Stripe work happens in **test mode** until the go-live gate. Test mode is
fully sandboxed — no EIN, no SSN, no real money. The line that must not be
crossed: **do not run real pilot transactions in live mode under a personal SSN
or an existing company's EIN.** That would route money legally through an
individual rather than the new entity, undoing the separation this project
exists for. Going live means opening a **new, separate Connect platform account
under the new entity** — not migrating an existing one.

### How it is built (phase 6)

`app/services/stripe_client.py` is the **only door**. No route, task or webhook
handler calls Stripe directly, because the moment there are two places that can
charge a card, one of them is missing an idempotency key. `post()` refuses a
mutating call that carries neither a key nor an explicit `non_idempotent_reason`
saying why it cannot duplicate anything that matters — exactly one call signs
that second form (an Express onboarding link, which expires and moves no money),
so skipping the key is a greppable decision rather than an omission.

**Money moves for work that happened.** The owner is charged when the cleaner
marks the job complete, not when a bid is accepted. An ordinary cancellation
therefore costs nobody anything and needs no refund, and the refund path stays
reserved for something going wrong *after* the work was done.

- **One destination charge**, through Stripe's hosted Checkout: one
  `PaymentIntent` with `transfer_data` at the cleaner's account and
  `application_fee_amount` as the platform cut. Card details never reach this
  server. The fee comes **out of** the cleaner's price rather than being added
  on top, so the bid on screen is what the owner pays, and the cleaner's share
  is the remainder — one subtraction, never two roundings.
- **A payment is only true when Stripe says so.** The request that starts a
  checkout creates an intent to pay; the webhook reports the money moving.
  Receipts are queued from the second, never the first. The webhook is the one
  unauthenticated endpoint in the product and it refuses every delivery that is
  not signed for our secret — including all of them when no secret is set.
- **Refunds are decided, not reversed.** `refund_application_fee` *and*
  `reverse_transfer`, both explicit: without the second the owner is made whole
  out of the platform's balance while the cleaner keeps the full amount, and
  nothing errors. Full refunds only at v1, admin-only, with a required reason —
  a partial refund has to decide how to split the shortfall, and that is a
  policy question with somebody's income on the other end.
- **Payout readiness is not the trust gate.** `cleaner_profiles.stripe_*` is
  deliberately outside `can_take_jobs`. Being trusted in a stranger's house and
  being able to receive a transfer are different questions; folding Stripe into
  the generated column would let a verification delay silently stop a vetted
  cleaner from bidding. The payment step refuses out loud instead, naming what
  is missing.
- **No key configured means the payment path is off, not faked.** Different
  from Checkr (manual fallback) and SMTP (log instead of send), on purpose: a
  human can run a background check by hand and a log line can stand in for an
  email. Nothing stands in for money, and no row may claim to have collected
  something it did not.

The Stripe client is **not verified against the live API** — same honesty as the
Checkr client. Its request shapes and response handling are covered by tests
against a recording fake, and the browser test drives a fake Stripe over real
HTTP (real form encoding, real redirect, real signed webhook), but no request
has ever been made to a real Stripe account from this codebase. Walk one payment
through with a test-mode key before relying on it.

---

## Carry-over test list

Written down from day one, built with their phases:

- ~~Double-award concurrency test (guardrail 1) — two simultaneous accepts, exactly one wins~~ — `tests/test_awards.py`, with a second test proving the check happens *inside* the lock rather than before it
- ~~No-show / late-cancellation re-post — turnover reopens urgent, owner and admin both alerted~~ — `tests/test_cancellation.py`
- ~~Stripe idempotency — replay the same charge attempt, assert no duplicate~~ — `tests/test_payments.py`, asserting the *same derived key* on both attempts rather than merely that a key was sent
- ~~Collected-vs-paid-out reconciliation — collected always equals payout + platform fee, no drift~~ — `payments.reconcile()`, asserted across a range of prices and after a refund
- ~~Refund path — fee and transfer both resolve, nothing left stranded~~ — `tests/test_payments.py::TestRefunds`, asserting `refund_application_fee` and `reverse_transfer` on the wire
- ~~Mutual review reveal — a one-sided review never shows before the other side submits or the timeout passes~~ — `tests/test_reviews.py`, plus a browser test asserting the *other side's screen does not change* when a review is written
- ~~A real click-through of bid → award → payment, not just "the button renders"~~ — bid → award → back out is `tests/e2e/test_award_flow.py`; bid → award → done → paid is `tests/e2e/test_payment_flow.py`, against a Stripe that answers over real HTTP

---

## Working in this repo

```bash
# backend
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
alembic upgrade head
uvicorn app.main:app --reload      # http://localhost:8000

# tests (needs Postgres — see backend/tests/conftest.py)
pytest

# frontend
cd frontend
npm install
npm run dev                        # http://localhost:5173, proxies /api to :8000
```

Conventions:

- Money is integer cents. Never a float.
- Derived fields (`can_take_jobs`, `urgency`) have exactly one author. Never
  re-derive one inline; call the function that owns it.
- Owner-scoped rows are fetched by id **and** owner in one query, never fetched
  then checked. Something that belongs to someone else answers 404, not 403 — a
  403 confirms the id exists. The same applies to a cleaner's own documents.
- The session uses `expire_on_commit=False`, so **an already-loaded collection
  survives a commit unchanged.** Build a response after a write from a freshly
  read object (`_fresh_detail`, `_entry_after_write`), and never call
  `expire_all()` *before* a commit — it discards the un-flushed change the
  commit was supposed to persist. Both of those were real bugs here.
- Timestamps are timezone-aware UTC (`TIMESTAMP WITH TIME ZONE`).
- Enums live in `app/models/enums.py` as Python `str, Enum` and as native
  Postgres enum types. Adding a value means a migration.
- Every schema change gets an Alembic migration. Never edit a migration that has
  already run anywhere but your own machine.
- One Alembic head. Two heads only fail at deploy time, which is the worst place
  to find out.
