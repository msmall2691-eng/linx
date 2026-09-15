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
| `property_calendars` | A booking feed an owner connected, and how its last read went |
| `disputes` | A complaint about a job, decided by a human |
| `reviews` | Mutual, delayed reveal |
| `payments_in` | Collects from the owner |
| `payouts` | Pays the cleaner |
| `task_runs` | One row per background task: when it last *finished* |

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

### Several jobs at once, for the owner a feed cannot serve

A booking calendar is the fast path onto the board, and **two kinds of owner
cannot use one at all**: a home has no booking calendar — that is what a home
is — and a rental booked direct or by phone has no `.ics` URL to paste. Both
were left typing one job per screen, which is fine for one job and absurd for a
season. `turnovers.create_many` is the answer, reached from the jobs list
rather than from a calendar panel those owners never open.

It is deliberately **not a second calendar source**. It writes no
`property_calendars` row, claims no `external_ref`, and is never reconciled
against anything afterwards: the owner said these dates once, and from then on
the rows are ordinary turnovers they own. Everything in `calendars.py` about
identity, adoption and vanishing bookings exists because a feed keeps
*talking*; a list somebody typed does not, and borrowing that machinery would
have meant maintaining rules with nothing behind them.

- **Drafts, never posted, and not a setting.** Calendars rule 1 in its other
  spelling: an owner who wanted ten jobs on the bench can post them in a
  minute, and one who pasted the wrong column cannot unsend the alerts, the
  bids, or the apology. There is no `publish` flag at all — a flag that
  silently did nothing would be worse than its absence. Drafts also notify
  nobody, which is why this added no `NotificationEvent`; the closed list stays
  closed.
- **All or nothing.** A mismatch is refused rather than corrected, as
  everywhere else, and on a list that has to mean the whole list: a partial
  write leaves the owner comparing what they pasted against what landed, with
  no retry that is not itself a duplicate. The refusal names the row.
- **`service_type_for` and `checkin_for` are asked, not restated**, so a home
  still refuses a checkin here — per row, with the row named — and the property
  row is locked for the whole submission, because one answer about its type has
  to hold across the list.
- **A duplicate is reported, not refused and not repeated.** The owner asked
  for a job that is already there, which is not a mismatch; but dropping it
  silently is how somebody pastes twice and books two cleaners for one clean.
  `already_there` carries the dates back and the screen shows them. A
  *cancelled* job does not block re-entering its date — an owner who called a
  job off and is re-entering it means it.
- **`MAX_BULK_JOBS` exists because the failure is not a big request**, it is an
  owner who meant six jobs and posted six hundred.

**An uploaded `.ics` fills in that form and writes nothing.** For the owner
whose listing site exports a file but will not hand over a sync URL:
`/properties/{id}/calendars/read-file` runs the same `parse` then `jobs_for` the
sync path runs — so a file and a URL cannot disagree about what a booking
implies — and returns proposed rows. It asks `refuse_ineligible`, so a home
gets the same refusal in the same words whichever way the calendar arrives, and
it enforces `MAX_FEED_BYTES` before reading rather than after. It is the one
way to read a calendar here **without this server connecting anywhere**, so
none of the request-forgery surface a URL carries applies; there is a test that
the path opens no socket.

### Booking calendars — a projection, not a source of truth

An owner connects the .ics their Airbnb or VRBO listing already publishes, and
each checkout in it becomes a **draft** turnover they confirm.
`app/services/calendars.py` owns all of it, and every rule there follows from
one sentence: **linx owns the turnover; the listing site owns the booking.**

1. **A booking becomes a draft, never a live job.** An owner who wanted ten jobs
   posted can do that in a minute; an owner who did not cannot unsend the
   alerts, the bids, or the apology.
2. **A row a person has touched is theirs.** Sync updates a *draft* it wrote and
   nothing else; `calendars._belongs_to_the_feed` is both halves of that in one
   place, so the status test and the person test cannot drift apart.

   The person test is **`turnovers.owner_edited_at`, an explicit marker written
   where people edit** — not a comparison against `updated_at`. That comparison
   was the first design and it was wrong for a reason worth keeping written
   down: `updated_at` moves for *any* write, and the read paths write.
   `refresh_urgency` persists a standing vacancy's climb up the urgency ladder,
   which is exactly what a synced draft with no next guest is — so merely
   opening the turnover list stamped the draft as edited, and from then on the
   feed could neither correct its dates nor withdraw it when the guest
   cancelled. Rule 2 switched off by a page view, with nothing failing to say so.
3. **Vanishing from the feed is not permission to delete.** An untouched draft
   goes, because nothing was staffed for it. A posted or awarded job stays and
   is *reported* — a guest cancelling does not get to cancel a cleaner.
4. **Identity is the event's UID *and* its RECURRENCE-ID, not its dates** —
   unique on `(source_calendar_id, external_ref)`. **It survives the calendar
   being removed**: deleting a feed is `ON DELETE SET NULL` because a job
   outlives the calendar that proposed it, and re-adding the same feed is the
   *documented* way to change its URL — so `reconcile` adopts an orphan on the
   same property carrying the same event id **and the same
   `turnovers.source_feed_key`** rather than proposing the booking a second
   time. Without adoption, remove-and-re-add turned one stay into two jobs;
   without the feed key, adoption reached too far — UIDs are arbitrary
   feed-local strings, so two listings on one property whose feeds reuse one
   would hand each other's jobs over. The key is a digest rather than the URL
   because the URL is a credential. A booking whose dates move is
   one booking; without that it becomes a second job while the first sits
   orphaned. The RECURRENCE-ID half is iCalendar's own rule rather than a
   workaround: an overridden occurrence legitimately repeats its parent's UID,
   and reading the UID alone made two events one identity — so both rows were
   inserted and the *commit* failed, surfacing as a 500 with no reason recorded.
   It is formatted from the value, never `str()` of the library's object, since
   identity that moves between library versions orphans everything keyed to the
   old spelling. Past that pair, a repeat is a feed we cannot interpret: the
   first is kept and the rest logged, because inserting both is a crash. An
   identity too long for `external_ref` is **hashed, never truncated** — two long
   UIDs sharing a prefix would truncate to the same identity, which is the one
   thing identity may never do.
5. **An unreadable feed changes nothing.** "The feed is empty" legitimately
   deletes drafts, so "the feed did not load" must never look the same. A
   failure is recorded on the calendar row and every turnover is left alone.

Two things the feed cannot tell us, and where they come from instead:

- **The time.** Exports are all-day — a guest leaves "on the 7th" with no hour —
  and the urgency ladder is measured in hours. `properties.default_checkout_time`
  and `default_checkin_time` are the house's own policy, which the owner knows
  and the calendar does not.
- **Which arrival is *this* clean's checkin.** Only one close enough behind the
  departure to be the job — `NEXT_STAY_WITHIN`, which *is* `SOON_WITHIN` rather
  than a number of its own so the rule and the ladder it feeds cannot drift.
  A stay three weeks later would otherwise make a checkout twelve hours away
  read `standard`, because the measure becomes the length of a three-week window
  instead of how soon the job is.
- **Which departures are still jobs.** Exports keep their history and the
  horizon only bounded the future, so there is a floor as well
  (`PAST_TOLERANCE`): without one, connecting a calendar proposes an overdue,
  `urgent` draft for every stay the listing ever had.
- **Whether an event is a stay.** Airbnb sends the owner's blocked dates through
  the same feed. `calendars.BLOCK_MARKERS` is a substring heuristic and is
  labelled as one; it is allowed to be a heuristic because its failure mode is a
  draft the owner deletes.

**A feed URL is secret the way a link is secret** — anybody holding it can read
the booking dates for somebody's house. It is owner-only, on no cleaner-facing
or admin shape, and there is a test that it appears nowhere in a board response.

**A log file is not an exception to that.** httpx logs the full request URL at
INFO and listing sites put the token in the path or query, so every unattended
sync wrote every owner's credential into the application log until
`calendars.py` turned that logger down. It is a module-level global on purpose:
the leak happens in two processes (the web app for the Sync button, the
scheduled task) and this module is imported by both, so configuring it at each
entry point would be two places to keep in step.
The URL is also not editable in place: changing it would keep the calendar's id
while pointing it at different bookings, so every turnover keyed to it would
claim a source it never came from.

**It is also a place this server connects to**, which makes the field a request
forgery primitive unless it is guarded. Two rules, both in `calendars.py`:

- **Every hop is checked, not just the URL the owner typed.** The guard lives in
  the HTTP transport (`_PublicOnlyTransport`), because a perfectly public host
  answering `302` to `http://169.254.169.254/` is the whole attack rather than
  an edge case. Anything that does not resolve to a global address is refused —
  and the residual DNS-rebind window is named in the code rather than papered
  over.
- **`MAX_FEED_BYTES` is enforced while the body streams**, never on
  `len(response.content)` — reading the whole thing before measuring it is not a
  limit at all, and this path runs unattended for every owner on every pass.
- **`MAX_FEED_SECONDS` is a second, separate limit**, because the byte cap does
  not bound time and `FETCH_TIMEOUT_SECONDS` is per-receive inactivity rather
  than a total deadline: a host dribbling one small chunk every nineteen seconds
  trips neither. That matters here more than elsewhere, since the scheduled pass
  reads feeds **serially and first** — one slow feed would stop the reminders,
  the unclaimed alarm, the outbox and the review reveal for everybody.
- **The stored URL is the one that will be fetched.** `CalendarCreate`
  canonicalises the request identity — fragment dropped, scheme and host
  lowercased, an explicit default port removed, an empty path written as `/` —
  because
  `uq_property_calendars_property_url` compares *strings* while the network
  compares *requests*, and one feed spelled three ways is three calendars and
  three drafts per booking. The path and query are left exactly as typed: those
  are case-sensitive, and listing sites put a token in one of them.
- **A `STATUS:CANCELLED` event is not a stay.** Providers may keep a cancelled
  reservation in the feed rather than dropping it; read as live it becomes a
  draft for a stay that is not happening, and on later syncs it still looks
  live, so the job is never even reported as vanished.

**A booking that vanishes from a job somebody is already on is reported, and the
report is stored.** `property_calendars.last_stale_kept` holds it, because the
pass that normally finds it is the scheduled one, which has no screen to answer
— a number returned only to a button nobody pressed is a warning the product
promised and then dropped. Whether it should also *notify* is a genuine open
question rather than an oversight: `NotificationEvent` is a closed list, and
opening it is meant to be a decision somebody makes out loud.

**`sync()` is an explicit sequence, and the sequence is the design.** Four
rounds of review on this feature produced findings that were all really one
finding — *a step in the wrong place* — so the function is now written as the
order itself:

1. **Gate, before touching the network.** `_gate(calendar, prop)` is one
   predicate over both rows: the feed switched on, and the property still a live
   short-term rental. An archived property costs no outbound request.
2. **Fetch, under a deadline the caller can rely on** (`_fetch_within`).
3. **Claim — lock and re-read both rows, then gate again** with the same
   predicate. The answer can change while we are on the network.
4. **Reconcile and record**, inside that lock.

The single-author rule (`refuse_ineligible`, which the add endpoint also asks
rather than copying) took three rounds to arrive at, and each round the rule was
real, written down, and reachable around on exactly one path: first `sync` had
it and `active_calendars` did not; then `active_calendars` had it and the Sync
button did not; then the add endpoint carried its own copy covering only the
residential half. **Asking one predicate at both moments is what retired the
class** — if an answer can change, it must be asked before *and* after, and it
must be the same question both times.

**`_claim` needs the lock and `populate_existing`, and the second is easy to
miss.** A locking `SELECT` takes the lock but still hands back the instance
already in the session's identity map, *with its old attribute values* — so
without it the row is locked and then read stale, which is the whole failure.
Property first, then calendar, always: a consistent lock order is what stops two
paths that take both from deadlocking. `update_property` takes the same row lock.

**The newest read wins, and the lock does not decide that.** A lock serialises
writes; it says nothing about which snapshot is current. A manual sync and the
scheduled pass can overlap, and the slower fetch commits second holding *older*
bookings — which would recreate a draft the newer pass correctly removed, or put
moved dates back. `sync` records when its fetch began and discards a snapshot
older than the calendar's last successful read.

**`property_calendars.sync_epoch` is the whole of that**, and it is an integer
rather than a clock on purpose. Three review rounds went into doing this with
timestamps — which one to store, which one to compare, whether a failed read
counts — and each answer produced the next question, because comparing wall
clocks across two processes is the wrong primitive for "has anything happened
since I looked?". Plain optimistic concurrency instead: note the epoch before
fetching, and under the lock either it is unchanged (commit, bump it) or this
snapshot is stale by definition. The same comparison guards the failure path, so
an older failure cannot bury a newer success and send the owner looking for a
problem that is already over.

**Every `CalendarError` out of `sync` leaves its reason on the row**, from any
step, via one handler that rolls back first. A gate refusal once escaped the
recording block and a caller swallowed it assuming a reason had been written —
the owner got a success with no jobs and no explanation. A uniform handler makes
that impossible rather than fixed, and the rollback matters now that it covers
reconcile: a failure partway through must not commit half a sync alongside its
own error message.

**The fetch deadline runs on a worker thread and *ends* the work, because a
blocking socket read cannot be cancelled.** `MAX_FEED_SECONDS` inside the
streaming loop does not bound the call: `client.stream()` must receive the whole
response *head* first, and httpx's read timeout is per-receive inactivity, so a
host trickling header bytes holds the call open indefinitely — tying up the
request worker behind the Sync button and stalling the scheduled pass, whose
budget is only checked *between* feeds.

**Stopping waiting is not the same as stopping**, and the first version of this
stopped there and called the leftover thread an acceptable residual. It was not:
for a header-trickler the body deadline is never reached, so no timeout ever
fires for that thread, and every scheduled pass and every press of the button
starts another that also never ends — an unbounded leak with a scheduler feeding
it. The deadline therefore **closes the client**, which closes the socket under
the blocked read and makes it raise. There is a test against a real server that
dribbles header bytes forever, asserting the worker is gone rather than merely
no longer waited on.

**And the population is capped outright** (`MAX_CONCURRENT_FETCHES`), because
closing the client still cannot interrupt a worker inside `socket.getaddrinfo`
— bounded by the OS resolver rather than unbounded, but able to outlive the
grace period. Chasing each way a worker might outstay its deadline is a losing
game; bounding how many can exist is not. The permit is held by the *thread* and
released when it ends, so what is counted is live workers rather than live
callers.

**The stale warning is only counted while it is still true.** A completed or
cancelled job, or one whose checkout has passed, has no booking in the feed
either — counting those would make `last_stale_kept` climb on every sync until
"a guest cancelled and a cleaner may still be coming" mostly described last
month's finished work, and a warning that is usually wrong is one nobody reads.

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
- **Disputes go to a human inbox, not a bot, at v1.** Built in phase 8
  (`app/services/disputes.py`), and the shape follows from that sentence:
  - **Three statuses and no `rejected`.** The outcome is `resolution_notes` —
    prose an admin wrote — because at this size the outcomes are not an
    enumerable set, and pretending otherwise puts a policy in a column.
    `acknowledged` is separate from `open` because "somebody has picked this
    up" and "this is settled" are different facts, and an admin working a
    backlog needs to tell what they have already read.
  - **Raising one tells an admin and the person who raised it — and nobody
    else.** The receipt is not a courtesy: somebody who reports that a stranger
    was in their house and hears nothing assumes it went nowhere. The *other
    side is deliberately not told*, because at this size a human decides when
    to involve somebody in a complaint about them, and a system that forwards
    it automatically has replaced the judgement the inbox exists for. They hear
    at resolution, with the note.
  - **Filing one changes nothing about the money or the booking.** No refund,
    no cancellation, no rating moves. That is the policy, not a gap.
  - **Either party may raise one on a job that went wrong**, including a
    cancelled award — `disputes.award_for` reads an award whether it is live or
    not, where `reviews.participants` insists on a completed one. The jobs most
    worth complaining about are the ones that did not finish, which is also why
    neither panel is gated on completion the way the review beside it is.

    Showing cancelled work also made `turnover_id` stop identifying a card: a
    cleaner who backs out and later wins the same job again has two, so the
    splice that keeps the screen alive after "I'm on site" matches on
    `award_id`. Keyed on the turnover it overwrote both, erasing the history
    card and leaving two React keys the same.

    That reachability is a rule in its own right, because it was broken on both
    screens at once and neither looked broken. The owner's panel was gated on
    `turnover.award`, which aliases `live_award` and so is null the moment a
    booking is cancelled; the cleaner's list does not ask for cancelled
    bookings, so the card carrying their panel vanished as they backed out.
    Both now mount unconditionally and let the server decide — the panel
    self-hides when there is nobody to dispute with — and a browser test raises
    a dispute from each side of a cancelled booking.
  - **The person filing says which booking**, and the server refuses rather
    than guessing (`disputes.award_under_dispute`). Freezing the award stops a
    dispute *changing* who it is about; it does not make an inferred choice
    right in the first place. An owner whose cleaner cancelled and whose job
    was re-awarded before they got round to complaining would have had their
    complaint filed against the replacement, who has done nothing — and the
    same happens to a cleaner who cancelled, re-bid and was booked again, two
    awards both theirs. `DisputeIn.award_id` is optional only because most
    turnovers have exactly one booking and asking a question with one possible
    answer is noise; with several, the refusal is the house rule from
    `service_type_for`. `DisputesOut.bookings` is what the screen renders the
    choice from.
  - **Working a dispute is serialised on the row; raising one is not.** That
    asymmetry is the difference between a duplicate a human closes and a record
    that disagrees with what the parties were told. Unlocked, two admins each
    checked a status they had loaded independently: an acknowledge committing
    after a resolve wrote `acknowledged` back over a settled dispute while
    leaving the note on it, and two resolves both passed, so the row kept the
    *last* admin's note while the dedupe key had already sent the *first* one.
    `disputes._claim` is guardrail 1's shape applied to a state transition,
    `populate_existing` included, and there is a real two-thread test rather
    than a sequential stand-in.
  - **A dispute is bound to one award, written when it is filed**
    (`disputes.award_id`), and `parties_of_dispute` is the only reader of it.
    "The award on this turnover" is a question with a different answer next
    week: a cancellation re-posts the job and the next accept writes a second
    `Award`. Recomputed at read time — which is how this was first built — every
    existing dispute silently re-pointed at the replacement cleaner, so the
    console showed an uninvolved person's name and phone number on somebody
    else's complaint, resolving emailed them about it, and the cleaner who
    raised it got a 404 on their own dispute. Nothing failed; the rows were all
    valid and described the wrong person. `award_for` therefore resolves the
    party **per person** — a cleaner is party to their own awards and nobody
    else's, the owner to the most recent — so a superseded cleaner can still
    complain about the job they lost.
  - The duplicate guard is **a courtesy, not an invariant**, and says so: two
    simultaneous submissions could both pass it, and the cost is one extra card
    in a queue a human closes. It deliberately takes no row lock, because a lock
    would imply an atomicity this path does not need.
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
- Dispute raised → an admin **and the person who raised it** (added in phase 8)
- Dispute resolved → both parties, carrying the admin's note

**All fifteen have a sender.** Nothing is declared-and-unwired any more;
the test that guarded that has flipped to guarding the other direction — a new
enum value with nothing behind it fails, which is the conversation adding one is
supposed to start.

**The list has grown twice, both times on purpose.** `job_completed` was added in phase 6
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

Four things hang off the clock rather than off something a person did: the
day-of reminder, the unclaimed alarm, the review reveal, and reading the booking
calendars. The calendar pass gives each feed its own `try` — one listing site
having a bad afternoon is exactly when every *other* owner's sync most needs to
keep working.

**Calendars run last in `run()`, and that order is load-bearing.** Reading feeds
is the only step that waits on somebody else's server, so everything that owes a
person something — the reminders, the unclaimed alarm, the review reveal, the
outbox drain — goes first and cannot be delayed by the network at all. It is
safe to reorder precisely because a synced booking becomes a *draft*, and a
draft notifies nobody. The pass also has a whole-pass budget
(`CALENDAR_PASS_BUDGET`) on top of the per-feed one, since otherwise the worst
case is the number of feeds times the per-feed deadline; feeds skipped by the
budget are first in line next time, because `active_calendars` is
oldest-sync-first.
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
  a checkbox. **`create_many` is deliberately not the thin end of that**: it
  writes the dates it was given and then forgets it did, with no rule, no
  series and nothing to amend later. "Every other Tuesday until I say stop" is
  still the decision above, and typing twelve dates is a way to live without
  it, not a way to sneak it in.
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
| 8 | Admin console — vetting queue, dispute inbox, unclaimed alerts, ledger | **done** |
| 9 | Pilot launch checklist — new Connect platform account under the new entity | **done** |

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

**Phase 9 enforces that sentence rather than only stating it.** A live key
(`sk_live_…` or `rk_live_…`) with `STRIPE_PLATFORM_ENTITY` unset makes every
mutating Stripe call refuse, in `stripe_client.post` — the only door. Prose does
not stop a live key being pasted into a service that is already working: that
change produces no error, fails no test, and surfaces at the end of a tax year
as money having moved through the wrong legal person.

**A refusal raised before the request leaves the process is a *known*
outcome**, and `StripeError.outcome_known` is what says so. Both handlers used
to key on `exc.status` — a proxy for "Stripe answered, so it rejected us and
nothing moved" — which is exactly right while the only two outcomes are
"answered" and "unreachable", and wrong for a local refusal, which has no
status while being the most definite outcome there is. Read through the proxy
the gate looked *unknown*, and guardrail 2 treats unknown as permanently
unsafe: a checkout would have parked in `requires_review`, which
`start_checkout` refuses to re-charge even after the variable is fixed, and a
refund would have turned an already-settled payment into money the ledger
reports as unknown. The gate protecting the entity would have made jobs
unpayable and revenue vanish off the books.

The variable holds the entity's **legal name**, not a boolean, because a
confirmation flag is a box anybody ticks and a name is a sentence somebody has
to mean. It cannot verify the name is true — nothing in this process can read
whose EIN a Stripe account was opened under — so the launch check reports it as
*declared* and says as much. Test mode is untouched; the gate can only ever
refuse to move real money.

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

### The admin console (phase 8)

`app/api/routes/console.py`, mounted under `/admin` beside the vetting queue
phase 3 built rather than absorbing it — the trust gate has one author and the
console reads it rather than re-deciding it.

**The console carries no definitions of its own.** Every number on it is read
from the function that already owns it, because a screen an admin trusts that
disagrees with the alert an admin was sent is worse than either alone: both
become untrustworthy and there is no way to tell which is lying.

- The unclaimed list is `turnovers.unclaimed_alarming` — the same call the
  scheduled alarm makes, moved out of `app/tasks/scheduled.py` so the two
  cannot drift. `UNCLAIMED_LOOKBACK` moved with it and is re-exported from its
  old home, because a name that moves silently is a name somebody still imports.
  It calls `refresh_urgency` before serialising, like every other read path: a
  standing vacancy's rung is measured against *now* and climbs as checkout
  approaches, and an unclaimed job is very often exactly that, because a
  cancellation re-posts it. Read straight from the column, the queue whose
  purpose is sorting out what is most urgent showed whatever rung the job was
  last written at.
  Its bid count is `BidStatus.SUBMITTED` only: a job re-posted after a
  cancellation still carries the accepted bid and every declined one, so
  counting them all made the alarm read "3 bids, none accepted" — an owner
  dithering over offers — when there were no live offers and the real problem
  was that nobody had bid. An alarm that misdescribes the problem is worse than
  one that does not fire.
- Nothing on it links to `/turnovers/:id`. That route is `OwnerRoute`-wrapped
  and its endpoint is owner-only, so an admin clicking one was bounced to
  `/dashboard`; the console offered three drill-downs no console user could
  open. The names are plain text until there is an admin-readable detail view,
  because a link that cannot be followed reads as a screen that exists.
- The ledger sums its own rows rather than querying totals separately, and
  carries `total_drift_cents` from `payments.reconcile`. Two queries that could
  disagree about the same money is how a reconciliation screen ends up
  reassuring somebody about a number it did not check.
- **It reads `payments.ledger_figures`, not `reconcile` directly, because
  `reconcile` is settled-payment arithmetic.** `amount_cents` on a `pending` or
  `processing` row is an intention — the checkout the owner has not finished,
  or the webhook that has not arrived — and on a `failed` one it is an
  intention now known not to have happened. Reconciled anyway, an ordinary
  payment in flight has an amount and no payout, so the whole cleaner share
  read as drift: the financial alarm in the red for every normal checkout,
  which is how an alarm becomes something people scroll past. It also claimed
  money as collected that Stripe had not confirmed, on the one screen whose job
  is saying what actually moved.

  `payments.settled()` reads `status`, which means **`status` has to keep
  meaning "what happened to the collection"** rather than "what happened last".
  A refund that Stripe refuses used to write `failed` onto an already-settled
  payment, so the ledger read a row whose charge and payout both still existed
  as money never collected — zero revenue, zero fee, and the cleaner's payout
  reported as negative drift, every time a refund attempt was refused. A
  definitive refusal now restores `succeeded`, because the collection is still
  in force and only the refund failed; `failure_message` carries that, and an
  indeterminate one still parks in `requires_review` because there the whole
  amount's disposition is genuinely unknown. `payments.mark_failed` already
  held this principle for out-of-order webhooks — a success already recorded is
  not undone by a later failure notice — and it applies just as much to a
  failure this code writes about itself.

  `payments.settled()` is the single author for "has this been collected", and
  four buckets fall out of it, each meaning something different: **settled**
  (`succeeded`/`refunded`) gets the reconcile arithmetic; **in flight**
  (`pending`/`processing`) is carried as `awaiting_cents`, shown and never
  totalled as collected; **unknown** (`requires_review` — guardrail 2's flag,
  written before the network call) is carried as `unknown_cents`, because
  counting it as collected claims money nobody confirmed and counting it as
  zero writes off money that may well have moved; and **failed** is in none of
  them, reporting zero everywhere, because we were told it did not happen.
  Drift is still computed on unsettled rows rather than suppressed — a payout
  against a payment that never succeeded is money out with nothing in, which is
  exactly what drift is for.

### Launch readiness (phase 9)

`app/services/launch.py`, read two ways: `python -m app.tasks.launch_check` at
deploy time, and the console's **Launch readiness** panel — one function, so
the screen and the command cannot disagree. `docs/LAUNCH.md` is the long form.

**Four states, and the fourth is the design.** `ready`, `blocked`, `attention`,
and `unverifiable` — *this process cannot know*. The obvious two-state
checklist turns everything it cannot see into a pass and then reports all-green
for a system nobody has confirmed anything about, which is the same mistake as
assuming an unknown Stripe outcome succeeded. `is_launchable` counts an
unverifiable item as outstanding, so the summary can never say ready while
something is merely unexamined. Three items are permanently in that state: whose
entity the Connect account belongs to, whether anybody has walked a candidate
through Checkr, and whether a human actually works the dispute inbox.

**Set is not the same as usable, and truthiness is the proxy this module got
caught on three times.** `PUBLIC_BASE_URL=http://localhost:5173` is the value
the README documents for development and a perfectly truthy string; carried
into a deployment it made the check say Stripe can send people back while
`payments._app_base()` used it verbatim, so an owner finishing a payment and a
cleaner finishing onboarding both landed on their own computer.
`_unusable_base_url` parses it and requires a public https origin. It is
deliberately **not** `calendars._refuse_private_address`, despite the obvious
overlap: that one asks "will *our process* connect somewhere private" and
resolves every name to answer it, which is right for a request this server
makes and wrong here — a launch check that did DNS would call a deployment
unready because a new record had not propagated yet.

**The check asks the function that owns each rule rather than re-deriving it**
— the console rule from phase 8, for the same reason, and it was got wrong
twice. `_admin_exists` counted `role == ADMIN` while `notifications.admins`
also requires `is_active`, so a database holding only deactivated admins
reported that somebody receives the alerts while every one of them resolved to
an empty list: the check saying yes to precisely the failure it exists to
catch. `_payment_proven` hand-copied `{succeeded, refunded}`, which *is*
`payments.SETTLED_STATUSES` — latent only because the two agreed. Both now call
the owner, so the next change to either rule arrives here on its own.

The same mistake twice more, in its other two spellings. `SECRET_KEY` was
checked for inequality with the development placeholder — which is what
`config.py` checked too, so `SECRET_KEY=` and `SECRET_KEY=x` booted the service
and turned the check green while every JWT was signed with a guessable value,
and anybody holding an ordinary token could forge an admin one.
`config.weak_secret_key` is now the one author and both ask it; length is the
floor rather than an entropy measure, because nothing here can tell a random
string from a memorable one and a check that claimed to would be this module's
own lie. And `PUBLIC_BASE_URL` was checked as a URL when it is an *origin*:
`payments._app_base()` appends `/cleaner/profile` to it, so a query or fragment
does not sit where a path can follow, and `https://linx.example#preview` sends
the browser to the site root with the callback buried in the fragment — almost
right, which is worse than nowhere. The same setting was also read in two
spellings: the check stripped whitespace before parsing and `_app_base()` did
not, so a trailing space validated clean and then sat in the middle of a
Checkout return URL. It is normalised once in `config.py` instead, because
normalising in either reader is what lets a third arrive with a third opinion —
the same failure as one feed URL spelled three ways being three calendars.

**`SMTP_HOST` set is not `SMTP_HOST` working**, so that check has three answers
rather than two, the same shape as `_payment_proven`: no host blocks, a host
with a delivered notification behind it is ready, and a host nobody has
successfully sent through is `attention` — the honest description of a fresh
deployment. The evidence is scoped to the sender configured *now*
(`notifications.sent_via`, written from `delivery.Sender.identity` — every
setting that decides whether a delivery succeeds, with the credentials as an
HMAC under `SECRET_KEY` rather than plaintext, because this is read back onto an
admin screen and a password on a screen is a password in a screenshot — and
**keyed rather than merely salted**, since the host and port are printed beside
the digest and usernames are guessable, so a fast salted hash would have handed
anybody who could read the column an offline oracle for the SMTP password.
Rotating `SECRET_KEY` re-reads past deliveries as "a sender since replaced",
which is the conservative direction. The material is JSON rather than a
delimiter join, because `"|".join` is not injective — username `a|b` with
password `c` and username `a` with password `b|c` are the same bytes; host, port and
from-address alone left `SMTP_USERNAME`, `SMTP_PASSWORD` and `SMTP_USE_TLS`
able to change underneath it): a `SENT` row proves *a* sender worked,
so without that tie, changing `SMTP_HOST` to something broken left yesterday's
success standing as proof about a system nobody is using. An unreachable host, a refused credential or a sender address the
relay rejects all fail at send time and every notification sits `failed`, which
a check titled *Notifications are actually sent* used to report as ready.

**A connected account belongs to a platform, and the id does not say which.**
Stripe objects are mode-scoped: an `acct_…` created with a test key does not
exist to a live key. That is latent only while the platform never changes, and
the launch order changes it on purpose — walk a job end to end on the deployed
site in test mode, *then* set the live key. The three `cleaner_profiles.stripe_*`
fields were facts about some platform with nothing saying which, so every
cleaner would have carried a test-mode account with `stripe_payouts_enabled`
true, `payout_blocker` would have found nothing missing, and the first live
destination charge would have named an account that does not exist: the owner
charged and the transfer with nowhere to go, every row valid.
`stripe_account_livemode` and `stripe_platform_id` record it, and
`payments.connected_account_is_foreign` is their one reader — a NULL mode
counts as foreign, because not knowing which platform an account is on is not
knowing it is this one. **Two columns, because neither alone is the identity:**
a Stripe account keeps one `acct_…` across test and live, so the platform id
does not tell the modes apart, and two *different* platforms in the same mode
compare equal on the boolean — which the launch order itself reaches, since
step 4 opens a new platform account under the new entity and testing it first
means new test keys. `payments.platform_account_id()` resolves the current
platform once and returning None means *not known*: never a match, never a
mismatch, because a Stripe blip turning every vetted cleaner unpayable is the
opposite failure and just as bad. It caches a success and **deliberately not a
failure** — an outage must not become a permanent unknown for the life of the
process — so the caller that asks about many rows resolves once and passes the
answer down (`connected_account_is_foreign(..., platform=...)`, with an
`UNRESOLVED` sentinel because a resolved *None* is an answer rather than an
absence). Without that, an outage cost one full timeout per cleaner plus one
more, and the console hung for minutes on the screen whose job is saying what
is wrong. The charge path **refuses**,
naming the remedy; the onboarding path **replaces**, because there somebody is
deliberately connecting to the platform that is running now, and resetting the
two flags keeps them refused until they actually finish. Migration 0009 does
not backfill a guess.

**Everything checked fails silently.** Anything that shouts on its own — a bad
`DATABASE_URL`, a missing `SECRET_KEY` — already stops the boot in
`app/preflight.py` and is not given a second home. What is here is the other
kind: `os.path.ismount` on the upload directory, because a plain directory is
writable and passes every naive test and is replaced on the next deploy, so the
photo IDs vanish while their rows survive *one deploy after the mistake*; no
admin user, because "an admin" resolves from the role and with none every alert
is addressed to nobody; no `SMTP_HOST`, because every notification then stays
`pending` and an owner learns their cleaner cancelled by arriving at a dirty
house.

**`task_runs` exists because the scheduled pass is the only part of this product
that says nothing when it stops.** A cron service that was never created, or has
been erroring since the last deploy, is indistinguishable from a quiet week —
and the review reveal is load-bearing rather than a courtesy, so silence there
is how refusing to answer becomes the way to bury a bad review. Inference does
not work and that is worth writing down, because it is the obvious first idea:
the newest notification and the newest calendar sync are both silent on a
genuinely quiet pass, so "nothing happened" and "nothing ran" look identical.
The pass records it itself — one row, overwritten, written **after** the work,
because a pass that starts and dies is not evidence that anything was done.
The write is one `INSERT ... ON CONFLICT DO UPDATE` rather than select-then-
insert: this module promises the pass is safe to run as often as you like, and
a manual run overlapping cron on a first deploy is precisely when there is no
row yet — both would read `None`, both would insert, and the unique constraint
would fail one of them *at commit*, after it had done all its real work.

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
