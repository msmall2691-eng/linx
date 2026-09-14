---
name: seam-check-before-removal
description: What to do before deleting, disabling, bypassing, or short-circuiting anything that already exists in linx — a status transition, an approval gate, a permission check, a validation, a notification call, a status value, a field, a refusal. Load BEFORE writing the change, not after. Also trigger on "remove", "delete", "drop this check", "no longer needed", "unused", "dead code", "simplify this away", "bypass", "skip the step", "we don't need this anymore", "clean up", or any diff that is mostly red.
---

# Check the seam before you cut it

This is guardrail 3, and it is a process rule rather than a code pattern —
which is exactly why it needs enforcing. The failure it prevents is the most
repeated one in the project this codebase was separated from: **a change that
was correct on its own and wrong at the seam**. Something downstream had been
quietly relying on the removed step, kept working by accident for a while, and
then didn't — with nothing failing loudly at the moment of the change.

Removals do not announce themselves. Adding a field breaks a test; deleting a
notification call breaks nothing until a person is standing in a house nobody
cleaned.

## Before the change ships

1. **Grep for the thing itself** — the function, field, status value, event
   name, constant, endpoint, or CSS/test id being removed. Across `backend/app`,
   `backend/tests`, `frontend/src`, `backend/alembic`, and `CLAUDE.md`.
2. **Grep for the state it used to write.** This is the one people skip. If the
   step set `reopened_at`, `status`, `attempted_at`, `cancelled_at` or a flag,
   find everything that *reads* that — a filter, a badge, a sort order, a
   migration, a report.
3. **Look for tests that assert it happened.** A test asserting a 409, an alert,
   a redirect, a refusal. If you are editing such a test to make your change
   pass, that is the seam talking. Rewriting it may well be right — phase 4 did
   exactly that to the phase-2 test that refused to cancel an awarded turnover —
   but it is a decision to justify in the PR, never a formality.
4. **Write down what you found in the change description**, even when the answer
   is "nothing depends on this." The empty answer is the valuable one: it is the
   difference between "I checked and nothing depends on it" and "I didn't look."
5. **Decide explicitly what replaces it.** If something was relying on the
   removed step — a safety check, a notification, a visibility rule, a gate — it
   does not just silently disappear. Either the replacement is in the same
   change, or the PR says plainly why nothing is needed.

## What counts as a removal

More than `git rm`. All of these are removals for this purpose:

- deleting or renaming a field, status value, or enum member,
- an endpoint that starts accepting a state it used to refuse,
- a check moved behind a flag, a condition, or an early `return`,
- an `if` that used to raise and now logs,
- a notification call deleted "because phase 5 will do it properly",
- widening a permission or a response shape (what *stops* being withheld is a
  removal of a boundary),
- a test deleted, skipped, or loosened.

## Never

- **Never skip, disable, or quarantine a test to make a change land.** If a test
  is wrong, rewrite it deliberately and say why. If it is right, the change is
  wrong.
- **Never remove a refusal without naming who now has to notice.** The refusals
  in this codebase exist because of specific, named failures — a cleaner finding
  out by showing up; a page going blank after a 200.

## Enforce it

Run `seam-regression-auditor` (`.claude/agents/seam-regression-auditor.md`) on
any diff that removes or disables something. It greps for the dependents, flags
what looks load-bearing, and checks that the change description actually
addresses what it found rather than saying "removed unused code."
