"""Is this ready to take real money from real people?

**Phase 9 is a checklist, and this repository does not trust checklists.** A
list in a README is a list somebody reads once, on the day they write it. Every
other rule here is paired with something that checks it, so this one is too:
the checks run, they read the live configuration and the live database, and
they refuse to call anything ready that they could not actually verify.

Four states, and the fourth is the one that matters:

* `READY` — checked, and true.
* `BLOCKED` — checked, and false. A pilot with real people must not start.
* `ATTENTION` — true, but with something a person should know before launching.
* `UNVERIFIABLE` — **this process cannot know.** Whose EIN a Stripe account was
  opened under is not a fact any code here can read.

`UNVERIFIABLE` exists because the obvious design is worse: a checklist that
only reports pass or fail turns everything it cannot see into a pass, and then
reports all-green for a system nobody has confirmed anything about. That is the
same failure as assuming an unknown Stripe outcome succeeded, and it is
rejected here for the same reason. `is_launchable()` counts an unverifiable
item as outstanding, so the summary line can never say "ready" while something
is merely unexamined.

The checks are deliberately about **silent** failures. Anything that shouts on
its own — a bad `DATABASE_URL`, a missing `SECRET_KEY` — already stops the boot
in `app/preflight.py` and does not need a second home. What is here is the
other kind: a mail host nobody set, so every notification sits `pending` and
two people find out by turning up; a storage directory that is not a volume, so
the vetting IDs vanish on the next deploy while their rows survive; a cron
service nobody created, so one-sided reviews are never revealed and refusing to
answer becomes the way to bury a bad one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import DEV_SECRET_KEY, settings
from app.models.payment import PaymentIn
from app.models.task_run import TaskRun
from app.services import notifications, payments, stripe_client

#: The name the scheduled pass records itself under.
SCHEDULED_TASK_NAME = "scheduled"

#: How stale the scheduled pass may be before it counts as not running. The
#: README suggests every fifteen minutes; this allows a generous multiple of
#: that, because the check is meant to catch "it is not running at all", not to
#: complain about a slow afternoon.
SCHEDULED_MAX_AGE = timedelta(hours=2)


class State(str, Enum):
    READY = "ready"
    BLOCKED = "blocked"
    ATTENTION = "attention"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class Check:
    """One thing that has to be true, and what to do when it is not."""

    key: str
    title: str
    state: State
    #: What was found, in a sentence. Rendered verbatim.
    detail: str
    #: What to do about it. Empty when there is nothing to do.
    remedy: str = ""


def _check(key, title, ok, *, when_true, when_false, remedy, fail=State.BLOCKED):
    return Check(
        key=key,
        title=title,
        state=State.READY if ok else fail,
        detail=when_true if ok else when_false,
        remedy="" if ok else remedy,
    )


# --------------------------------------------------------------------------
# The environment this is running as
# --------------------------------------------------------------------------


def _environment() -> Check:
    return _check(
        "environment",
        "Running as production",
        settings.environment == "production",
        when_true="ENVIRONMENT=production.",
        when_false=f"ENVIRONMENT={settings.environment}.",
        remedy="Set ENVIRONMENT=production on the service.",
    )


def _secret_key() -> Check:
    return _check(
        "secret_key",
        "Signing key is a real one",
        settings.secret_key != DEV_SECRET_KEY,
        when_true="SECRET_KEY is set to something other than the development value.",
        when_false="SECRET_KEY is still the development placeholder.",
        remedy='python -c "import secrets; print(secrets.token_urlsafe(48))"',
    )


def _cors() -> Check:
    origins = settings.cors_origin_list
    return _check(
        "cors",
        "CORS is not open to everything",
        "*" not in origins,
        when_true=f"Allowed origins: {', '.join(origins) or 'none'}.",
        when_false="CORS_ORIGINS is '*', so any site can call this API with a "
        "signed-in person's browser.",
        remedy="Set CORS_ORIGINS to the deployed origin.",
    )


def _document_storage() -> Check:
    """**Is the upload directory actually a mounted volume?**

    The README says it must be, and nothing has ever checked. A container
    filesystem is replaced on every deploy, so without a volume the uploaded
    photo IDs disappear while their database rows survive — the admin queue
    then points at files that are gone, and the failure appears one deploy
    after the mistake rather than at the moment of it.

    `os.path.ismount` is the real test. A directory that exists and is writable
    proves nothing about surviving a deploy.
    """
    path = settings.document_storage_dir
    if not os.path.isdir(path):
        return Check(
            "document_storage",
            "Vetting uploads survive a deploy",
            State.BLOCKED,
            f"DOCUMENT_STORAGE_DIR ({path}) does not exist.",
            "Create it, and make it a mounted volume.",
        )
    if not os.access(path, os.W_OK):
        return Check(
            "document_storage",
            "Vetting uploads survive a deploy",
            State.BLOCKED,
            f"DOCUMENT_STORAGE_DIR ({path}) is not writable.",
            "Fix the permissions on the mounted volume.",
        )
    if not os.path.ismount(path):
        return Check(
            "document_storage",
            "Vetting uploads survive a deploy",
            State.BLOCKED,
            f"DOCUMENT_STORAGE_DIR ({path}) is a plain directory, not a mount "
            "point. Uploaded IDs will disappear on the next deploy while their "
            "rows survive.",
            "Mount a Railway volume and point DOCUMENT_STORAGE_DIR at it.",
        )
    return Check(
        "document_storage",
        "Vetting uploads survive a deploy",
        State.READY,
        f"DOCUMENT_STORAGE_DIR ({path}) is a mount point and is writable.",
    )


# --------------------------------------------------------------------------
# Money
# --------------------------------------------------------------------------


def _stripe_configured() -> Check:
    return _check(
        "stripe_configured",
        "Stripe is configured",
        stripe_client.is_configured(),
        when_true="STRIPE_SECRET_KEY is set.",
        when_false="STRIPE_SECRET_KEY is not set, so the payment path is off. "
        "Nothing is faked and no row will claim to have collected anything — "
        "but nobody can pay either.",
        remedy="Set STRIPE_SECRET_KEY.",
    )


def _stripe_mode() -> Check:
    """**The line this project exists to hold.**

    Never `READY`. In test mode a pilot is not taking real money, which is a
    thing to know rather than a thing that is finished; in live mode the entity
    is *declared* and this process cannot confirm the declaration is true.
    Reporting either as ready would be the checklist saying yes to the one
    question it is least able to answer.
    """
    if not stripe_client.is_configured():
        return Check(
            "stripe_mode",
            "Live money moves under the right entity",
            State.ATTENTION,
            "No Stripe key, so nothing can move either way.",
            "",
        )

    entity = stripe_client.platform_entity()
    if not stripe_client.live_mode():
        return Check(
            "stripe_mode",
            "Live money moves under the right entity",
            State.ATTENTION,
            "Stripe is in TEST mode. Real money cannot move, which is the "
            "correct posture until the Connect platform account exists under "
            "the new entity.",
            "When going live: open a NEW, SEPARATE Connect platform account "
            "under the new entity — never migrate an existing one — then set "
            "STRIPE_PLATFORM_ENTITY to its legal name.",
        )

    if entity is None:
        return Check(
            "stripe_mode",
            "Live money moves under the right entity",
            State.BLOCKED,
            "Stripe is in LIVE mode and STRIPE_PLATFORM_ENTITY is not set. "
            "Every mutating Stripe call is refusing until it is.",
            "Set STRIPE_PLATFORM_ENTITY to the legal name of the entity the "
            "Connect platform account was opened under.",
        )

    return Check(
        "stripe_mode",
        "Live money moves under the right entity",
        State.UNVERIFIABLE,
        f"Stripe is in LIVE mode, declared to {entity}. **Nothing in this "
        "process can confirm that is the entity the account was actually "
        "opened under** — a person has to know it is true.",
        f"Confirm in the Stripe dashboard that the platform account's tax "
        f"identity is {entity}, and not a personal SSN or an existing "
        "company's EIN.",
    )


def _stripe_webhook() -> Check:
    return _check(
        "stripe_webhook",
        "Payments can be confirmed",
        bool(settings.stripe_webhook_secret),
        when_true="STRIPE_WEBHOOK_SECRET is set.",
        when_false="STRIPE_WEBHOOK_SECRET is not set, so every webhook delivery "
        "is refused. A checkout would start and no payment would ever be "
        "recorded as collected — the money moves at Stripe and this database "
        "never hears.",
        remedy="Create a webhook endpoint pointed at /api/stripe/webhook and "
        "set its signing secret.",
    )


def _public_base_url() -> Check:
    return _check(
        "public_base_url",
        "Stripe can send people back",
        bool(settings.public_base_url),
        when_true=f"PUBLIC_BASE_URL is {settings.public_base_url}.",
        when_false="PUBLIC_BASE_URL is not set, so return URLs are guessed from "
        "the incoming request. Behind a proxy that guess is wrong, and a "
        "cleaner finishing Express onboarding lands nowhere.",
        remedy="Set PUBLIC_BASE_URL to the deployed origin.",
    )


def _payment_proven(db: Session) -> Check:
    """Has a payment ever actually settled here?

    The carry-over note in CLAUDE.md is blunt about this: no request has ever
    been made to a real Stripe account from this codebase. A settled row is the
    evidence that changed, and it is worth asking the database rather than
    trusting a memory of having done it.
    """
    settled = db.execute(
        select(func.count())
        .select_from(PaymentIn)
        # `payments.SETTLED_STATUSES`, not a hand-written copy of it. The same
        # mistake as the admin count above, one function over, and currently
        # latent only because the two lists happen to agree: `settled()` is the
        # single author of "has this been collected", and a second spelling of
        # its set is a disagreement waiting for somebody to edit one of them.
        .where(PaymentIn.status.in_(tuple(payments.SETTLED_STATUSES)))
    ).scalar_one()
    return _check(
        "payment_proven",
        "A payment has settled end to end",
        settled > 0,
        when_true=f"{settled} payment(s) have settled in this database.",
        when_false="No payment has ever settled here. The Stripe client is "
        "written to the documented API and covered by tests against a fake; it "
        "has never been verified against Stripe itself.",
        remedy="Walk one job through with a test-mode key: bid, award, "
        "complete, pay, and confirm the webhook marks it collected.",
        fail=State.ATTENTION,
    )


# --------------------------------------------------------------------------
# The things that stop without saying so
# --------------------------------------------------------------------------


def _email_sender() -> Check:
    return _check(
        "email",
        "Notifications are actually sent",
        bool(settings.smtp_host),
        when_true=f"SMTP_HOST is {settings.smtp_host}.",
        when_false="SMTP_HOST is not set. Every notification is recorded and "
        "logged, and every one stays `pending`, because nothing is sent. "
        "Nobody is told anything — an owner learns their cleaner cancelled by "
        "arriving at a dirty house.",
        remedy="Set SMTP_HOST and its credentials.",
    )


def _scheduled_pass(db: Session, *, now: datetime | None = None) -> Check:
    """Is the cron service running?

    The one part of this product that says nothing when it stops. Three things
    hang off it and one is load-bearing rather than a courtesy: without the
    pass, one-sided reviews are never revealed, and refusing to answer becomes
    the way to bury a bad review.
    """
    reference = now or datetime.now(timezone.utc)
    run = db.execute(
        select(TaskRun).where(TaskRun.name == SCHEDULED_TASK_NAME)
    ).scalars().first()

    remedy = (
        "Add a cron service on the same image running "
        "`python -m app.tasks.scheduled`, every fifteen minutes. Running it "
        "more often is safe by design."
    )
    if run is None:
        return Check(
            "scheduled",
            "The scheduled pass is running",
            State.BLOCKED,
            "The scheduled pass has never recorded a run against this database.",
            remedy,
        )

    age = reference - run.finished_at
    if age > SCHEDULED_MAX_AGE:
        hours = age.total_seconds() / 3600
        return Check(
            "scheduled",
            "The scheduled pass is running",
            State.BLOCKED,
            f"The scheduled pass last finished {hours:.1f} hours ago, which is "
            "longer ago than it should ever be.",
            remedy,
        )

    minutes = age.total_seconds() / 60
    return Check(
        "scheduled",
        "The scheduled pass is running",
        State.READY,
        f"Last finished {minutes:.0f} minutes ago — {run.summary or 'no summary'}.",
    )


def _admin_exists(db: Session) -> Check:
    """Somebody has to be on the other end of every admin alert.

    `notifications` resolves the admin recipient from the *role* rather than a
    hardcoded address, precisely so alerting does not stop the day somebody new
    takes over the inbox. The other side of that: with no admin user at all,
    every admin alert resolves to nobody and is silently addressed to an empty
    list — the vetting queue, the unclaimed alarm, every dispute.
    """
    # **Ask the sender, do not re-derive its rule.** Counting `role == ADMIN`
    # was a second copy of the recipient rule that happened to be missing half
    # of it: `notifications.admins` also requires `is_active`, so a database
    # holding only deactivated admins made this check report ready while every
    # alert it is about resolved to an empty list — the check saying yes to
    # precisely the failure it exists to catch.
    #
    # Adding `is_active` here would have fixed today and left the second copy
    # in place. Calling the function means the next change to who counts as a
    # recipient arrives here on its own.
    recipients = notifications.admins(db)
    return _check(
        "admin",
        "Somebody receives the admin alerts",
        bool(recipients),
        when_true=f"{len(recipients)} active admin account(s).",
        when_false="There is no active admin user. Every alert addressed to "
        "'an admin' — vetting, unclaimed jobs, disputes, no-shows — resolves "
        "to nobody.",
        remedy="Create an admin user, or reactivate one.",
    )


# --------------------------------------------------------------------------
# The ones a person has to answer
# --------------------------------------------------------------------------


def _declared_only() -> list[Check]:
    """Things no code in this process can check, listed rather than assumed.

    They are here so the list is honest about its own edges. Leaving them out
    would make the check look complete while saying nothing at all about the
    decisions that carry the most consequence.
    """
    return [
        Check(
            "entity_separation",
            "The platform account is a new one under the new entity",
            State.UNVERIFIABLE,
            "Going live means opening a NEW, SEPARATE Connect platform account "
            "under the new entity — not migrating an existing one, and never "
            "running real transactions under a personal SSN or another "
            "company's EIN. That would route money legally through an "
            "individual and undo the separation this project exists for.",
            "Confirm the account was created fresh under the new entity.",
        ),
        Check(
            "background_checks",
            "The background-check vendor has been walked through once",
            State.UNVERIFIABLE,
            "The Checkr client is written to the documented interface and "
            "covered by tests against a mocked transport; no request has ever "
            "been made to a real Checkr account from this codebase. Without a "
            "key the manual provider runs and an admin records outcomes by "
            "hand, which is a valid launch posture — but it is a choice, not a "
            "default to arrive at by accident.",
            "Either walk one candidate through with a sandbox key, or decide "
            "out loud that launch is manual.",
        ),
        Check(
            "insurance",
            "Insurance and the dispute inbox have a human behind them",
            State.UNVERIFIABLE,
            "Insurance is a flag and not a gate at v1, and disputes go to a "
            "human inbox. Both are deliberate, and both assume somebody is "
            "actually reading and deciding.",
            "Confirm who works the dispute inbox, and how fast.",
        ),
    ]


# --------------------------------------------------------------------------
# The list
# --------------------------------------------------------------------------


def run_checks(db: Session, *, now: datetime | None = None) -> list[Check]:
    """Every check, in the order a person would work through them."""
    return [
        _environment(),
        _secret_key(),
        _cors(),
        _document_storage(),
        _admin_exists(db),
        _email_sender(),
        _scheduled_pass(db, now=now),
        _stripe_configured(),
        _stripe_mode(),
        _stripe_webhook(),
        _public_base_url(),
        _payment_proven(db),
        *_declared_only(),
    ]


def blocking(checks: list[Check]) -> list[Check]:
    """The ones that stop a pilot outright."""
    return [c for c in checks if c.state is State.BLOCKED]


def outstanding(checks: list[Check]) -> list[Check]:
    """Everything not finished — **including what could not be checked.**

    An unverifiable item counts as outstanding. That is the whole reason the
    state exists: a list that quietly treats "I could not look" as "fine" is a
    list that reports all-green for a system nobody has confirmed anything
    about.
    """
    return [c for c in checks if c.state is not State.READY]


def is_launchable(checks: list[Check]) -> bool:
    """Whether a pilot with real people can start. Never true with an
    unverifiable item outstanding — somebody has to answer it first."""
    return not outstanding(checks)
