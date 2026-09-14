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

  The "nobody has claimed this and checkout is tomorrow" alarm is deliberately
  **not** urgency. That is an operational alert with its own cutoff and its own
  recipients, and it belongs to the unclaimed-turnover path in phase 5.

---

## The privacy boundary

**A cleaner who has bid has not been hired.** The bench board therefore uses its
own response shapes (`app/schemas/board.py`) rather than filtering the owner's:

| Shown | Withheld until award |
|---|---|
| city, state, ZIP, beds/baths | street address |
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
- **Disputes go to a human inbox, not a bot, at v1.**
- **No-show / cancellation policy is defined before launch.** Built in phase 4
  (`app/services/awards.py`), and the definition is:
  - **Any** cancellation of a live award — the cleaner backs out, or never turns
    up — re-posts the job to the bench and alerts the owner, the cleaner and an
    admin immediately. Never conditional, never silent.
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
- Payment receipt (owner) / payout notice (cleaner)
- Review received (both directions, once visible)

Nine of those twelve are live. The last three — payment receipt, payout notice,
review received — are declared in the enum with no sender, because the state
transitions that fire them arrive in phases 6 and 7. Declared-and-unwired is
deliberate and tested as such; it is not the same as forgotten.

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

Two events hang off the clock rather than off something a person did: the day-of
reminder and the unclaimed alarm. `app/tasks/scheduled.py` is their entry point
(`python -m app.tasks.scheduled`), runs safely as often as you like because of
the dedupe key, and drains the outbox on the way past. Their two windows
(`REMINDER_HOURS_BEFORE`, `UNCLAIMED_ALERT_HOURS_BEFORE`) are separate settings
on purpose — one is a courtesy, the other an operational alarm, and the alarm is
still deliberately **not** a rung on the urgency ladder.

---

## Explicitly out of scope for v1

Do not build toward these:

- Non-STR recurring residential cleaning
- Automated dispute resolution
- Rating-weighted search ranking
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
| 6 | Stripe Connect, test mode end to end, refunds, reconciliation | not started |
| 7 | Mutual delayed-reveal reviews | not started |
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

---

## Carry-over test list

Written down from day one, built with their phases:

- ~~Double-award concurrency test (guardrail 1) — two simultaneous accepts, exactly one wins~~ — `tests/test_awards.py`, with a second test proving the check happens *inside* the lock rather than before it
- ~~No-show / late-cancellation re-post — turnover reopens urgent, owner and admin both alerted~~ — `tests/test_cancellation.py`
- Stripe idempotency — replay the same charge attempt, assert no duplicate
- Collected-vs-paid-out reconciliation — collected always equals payout + platform fee, no drift
- Refund path — fee and transfer both resolve, nothing left stranded
- Mutual review reveal — a one-sided review never shows before the other side submits or the timeout passes
- A real click-through of bid → award → payment, not just "the button renders" — bid → award → back out is `tests/e2e/test_award_flow.py`; payment joins it in phase 6

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
