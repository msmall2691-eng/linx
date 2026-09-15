# Pilot launch checklist

Phase 9. Two halves: the part a command checks, and the part only a person can
answer. Both are here because a checklist that hides its own edges is worse
than no checklist — it reports all-green for a system nobody has confirmed
anything about.

```bash
python -m app.tasks.launch_check
```

Exits non-zero when something is **blocking**. It deliberately does not exit
non-zero for the items it cannot check: a command that can never succeed is a
command somebody starts running with `|| true`. The same list is on the admin
console under **Launch readiness**, reading the same function, because a
checklist that is only a command is one nobody runs after the first week.

---

## The line

**Do not run real pilot transactions in live mode under a personal SSN or an
existing company's EIN.**

That is the sentence this repository is separate for. Routing money through an
individual rather than the new entity undoes the liability separation the whole
project exists to create, and it is not something you can tidy up afterwards —
it is what the 1099s say.

Going live therefore means opening a **new, separate Connect platform account
under the new entity**. Not migrating an existing one. Not switching an
existing account's bank details.

Since phase 9 this is enforced rather than merely written down. A live key
(`sk_live_…` or `rk_live_…`) with `STRIPE_PLATFORM_ENTITY` unset makes every
mutating Stripe call refuse, with a message naming the variable. Test mode is
untouched — the gate can only ever refuse to move real money.

`STRIPE_PLATFORM_ENTITY` is the entity's **legal name**, not a yes/no flag. A
confirmation checkbox is a box anybody ticks; a name is a sentence somebody has
to mean. Nothing in the running process can verify the name is true — no code
here can read whose EIN a Stripe account was opened under — so the check
reports it as *declared* and asks a person to confirm it in the dashboard.

---

## What the command checks

Each of these is here because it fails **silently**. Anything that shouts on
its own — a bad `DATABASE_URL`, a missing `SECRET_KEY` — already stops the boot
in `app/preflight.py` and is not repeated.

| Check | Why it is on the list |
|---|---|
| `ENVIRONMENT=production` | Development defaults are not a pilot posture |
| `SECRET_KEY` is real | Enforced at boot; restated so the report is complete |
| `CORS_ORIGINS` is not `*` | Any site could otherwise call the API with a signed-in person's browser |
| Uploads are on a **mounted volume** | `os.path.ismount`. A plain directory is writable and passes every naive test — and is replaced on the next deploy, so the photo IDs vanish while their rows survive. The failure appears one deploy *after* the mistake |
| An admin user exists | `notifications` resolves "an admin" from the role, on purpose. With none, every vetting, unclaimed, dispute and no-show alert resolves to nobody |
| `SMTP_HOST` is set | Without it every notification is recorded, logged, and left `pending`. Nobody is told anything — an owner learns their cleaner cancelled by arriving at a dirty house |
| The scheduled pass ran recently | See below |
| Stripe key, webhook secret, base URL | Without the webhook secret a checkout starts, the money moves at Stripe, and this database never hears |
| A payment has settled here | Evidence, rather than a memory of having tested it |

### The scheduled pass

The one part of this product that says nothing when it stops. A cron service
that was never created — or has been erroring since the last deploy — is
indistinguishable from a quiet week, and three things hang off it:

- the day-of reminder stops, and two people find out by turning up;
- the unclaimed alarm stops, and nobody hears that tomorrow's job has no cleaner;
- **one-sided reviews are never revealed**, which turns refusing to answer into
  the way to bury a bad review — the exact suppression the delay exists to
  prevent, achieved by doing nothing.

Inferring it from other rows does not work, and that is worth saying because it
is the obvious first idea: the newest notification and the newest calendar sync
are both silent on a genuinely quiet pass, so "nothing happened" and "nothing
ran" look identical. So the pass records it itself, in `task_runs` — one row,
overwritten, written **after** the work rather than before, because a pass that
starts and dies is not evidence that anything was done.

---

## What only a person can answer

These are reported as *needs a person* and count as outstanding. They never go
green on their own, and `launchable` is false while any of them is outstanding.

1. **The platform account is new, and under the new entity.** Confirm in the
   Stripe dashboard that the tax identity is the new entity — not a personal
   SSN, not another company's EIN.
2. **The background-check vendor.** The Checkr client is written to the
   documented interface and covered by tests against a mocked transport; no
   request has ever been made to a real Checkr account from this codebase.
   Either walk one candidate through with a sandbox key, or decide out loud
   that launch is manual — it is a valid posture, and it should be a choice
   rather than a default somebody arrives at by accident.
3. **Somebody works the dispute inbox.** Disputes go to a human at v1 and
   insurance is a flag rather than a gate. Both are deliberate, and both assume
   a person is reading and deciding. Decide who, and how fast.

---

## Order

1. Deploy with test-mode Stripe keys and work the list until nothing is
   blocking.
2. Walk one job end to end on the deployed site: post, bid, award, complete,
   pay, and confirm the webhook marks it collected. That turns *A payment has
   settled end to end* green and is the first time any code here has spoken to
   Stripe for real.
3. Answer the three items above.
4. Open the new Connect platform account under the new entity.
5. Set the live key **and** `STRIPE_PLATFORM_ENTITY` together. Setting the key
   alone is refused, by design.
6. Run the check once more against the live configuration.
