---
name: trust-gate-single-source
description: How linx decides whether a cleaner may be trusted with a stranger's house, and why that decision has exactly one author. Load BEFORE touching CleanerProfile, can_take_jobs, id_verification_status, background_check_status, has_insurance_on_file, the bidding gate, the accept-a-bid clearance check, the admin vetting queue, or any badge that says whether someone is verified. Also trigger on "can this cleaner bid", "vetting", "verified badge", "background check", "Checkr", "approve ID", "clearance", "why can't I bid", "let them bid anyway", "override the check".
---

# The trust gate has one author

A cleaner who can bid can end up alone in someone's house. The gate that decides
that is the most safety-critical boolean in the product, and the specific way it
goes wrong is not "somebody removes it" — it is **two copies of the rule that
drift apart**. One says verified, the other silently blocks bids, and nobody can
tell which one is lying or which one is load-bearing.

So: one author, two layers of enforcement, no overrides.

## 1. `can_take_jobs` is computed in one place, and not by us

It is a **Postgres generated column**:

```sql
id_verification_status = 'approved' AND background_check_status = 'approved'
```

No application code can write it. `UPDATE ... SET can_take_jobs` errors. There
is deliberately **no admin endpoint to override it**, because an override is
exactly how a cleaner ends up able to bid without a finished check — and the
override is always added for one sympathetic case at 9pm.

If the rule itself has to change (a third check, a different threshold), it
changes in the column definition, in a migration, and nowhere else.

## 2. `app/services/vetting.py` turns the boolean into a reason

`evaluate(profile)` reads `can_take_jobs` — it never recomputes it — and returns
the blockers, the warnings and a summary sentence. **Every surface that mentions
clearance calls it**:

- the bidding gate (`_require_cleared_profile` in `app/api/routes/board.py`),
- the re-check at accept time (`app/services/awards.py` — clearance can lapse
  between a bid and an accept, and a rejected check must not be able to walk
  into a house today),
- the cleaner's own profile panel,
- the admin queue.

The frontend renders the `vetting` object verbatim for the same reason. There is
a test asserting the bidding refusal is character-for-character the profile's
summary; that test is the seam, and it is supposed to be annoying to break.

**Never write a second version of the check** — not "just for the badge", not
"just for this one screen", not as a `bool(profile.id_status == "approved" and
...)` inline because the import felt heavy.

## 3. What the statuses mean, and what they may not do

- **ID verification is manual at launch.** A human looks at an uploaded photo ID
  and a reference. Do not automate it away at MVP; it is the safety valve
  between "hands off" and "anyone can walk into a stranger's house."
- **A background check is not an ID photo.** A photo confirms identity, not
  history. `background_check_status` gates bidding on its own.
- **`consider` is never an automatic rejection.** Checkr returns `clear` or
  `consider`; `consider` means a human must weigh something, and acting
  adversely on it has a legally defined process (FCRA adverse action). It parks
  in `pending` with a note. **Anything unrecognised also parks in `pending`** —
  an unknown vendor state must never read as a clearance.
- **No key configured means manual, not broken.** Without `CHECKR_API_KEY` the
  manual provider is used and an admin records the outcome. That is the launch
  posture.
- **Insurance is a flag, not a gate, at v1.** `has_insurance_on_file` is shown
  prominently when missing and does not block bidding. Requiring it up front
  while supply is scarce kills the launch. Do not quietly promote it to a gate;
  if that changes, it changes in the generated column with a migration.
- **Never demote an approval on a refresh.** The background-check refresh path
  may move `pending → approved`; it may not move `approved → anything`.

## 4. If you are about to widen the gate

Stop and say so explicitly in the PR. "This cleaner is stuck and needs to bid"
is a support problem with a support answer (finish their check), not a code
change. A one-off bypass merged under time pressure is indistinguishable, six
months later, from a deliberate design decision.
