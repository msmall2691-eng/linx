"""Collecting from the owner, paying the cleaner, and undoing both.

**One transaction, not two ledgers.** The owner is charged with a single Stripe
Connect *destination charge*: one `PaymentIntent` whose `transfer_data` points
at the cleaner's connected account and whose `application_fee_amount` is the
platform's cut. Stripe settles the split atomically, so the `Payout` row here is
a *record* of the transfer Stripe made — not a second call this code has to
remember to make. That is the whole reason the numbers cannot drift: collected
is always payout plus fee, by construction rather than by reconciliation.

Building it the other way — "collect from the owner" and "pay the cleaner" as
two independent transactions squared up later — leaves a window between them,
and that window is exactly where double-payments and silent drift live.

**Money moves for work that happened.** The charge is raised when the cleaner
marks the job complete, not when the owner accepts a bid. So an ordinary
cancellation costs nobody anything and needs no refund at all, and the refund
path is reserved for what it is actually for: something went wrong after the
job was done.

**Guardrail 2, at every call site.** Every key is derived from a row id already
in the database (`charge:award:{award.id}`), never generated per attempt, so a
genuine retry is recognized as a retry instead of billing the customer twice.
The attempt is written and committed *before* the network call, so a process
that dies mid-call leaves a row in `requires_review` — visibly unresolved,
never assumed successful and never assumed failed.

**Integer cents throughout.** The fee is integer arithmetic on the agreed price,
and the cleaner's share is the remainder, so the two always add back to exactly
what was collected. No float ever touches a number in this file.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.award import Award
from app.models.cleaner_profile import CleanerProfile
from app.models.enums import PaymentStatus, TurnoverStatus
from app.models.payment import PaymentIn, Payout
from app.models.property import Property
from app.models.turnover import Turnover
from app.models.user import User
from app.services import notifications, stripe_client

logger = logging.getLogger("linx.payments")


class PaymentRefused(Exception):
    """The payment cannot be raised in this state, and the reason is sayable."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


# --------------------------------------------------------------------------
# The split
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Split:
    """What the owner pays, what we keep, what the cleaner gets.

    Three integers that always satisfy `total == fee + cleaner`. The cleaner's
    share is computed as the *remainder* rather than as its own percentage,
    because two independent roundings are how a cent goes missing.
    """

    total_cents: int
    platform_fee_cents: int
    cleaner_cents: int


def split_for(agreed_price_cents: int, *, fee_bps: int | None = None) -> Split:
    """The cleaner named the price; the platform's cut comes out of it.

    The owner pays exactly what was agreed — no fee bolted on top, which would
    make the bid on screen a lie — and Stripe moves the remainder to the
    cleaner as part of the same charge.
    """
    if agreed_price_cents <= 0:
        raise PaymentRefused("There is no agreed price to charge.")

    bps = settings.platform_fee_bps if fee_bps is None else fee_bps
    # Integer division, deliberately truncating: a fraction of a cent rounds in
    # the cleaner's favour rather than ours.
    fee = (agreed_price_cents * bps) // 10_000
    return Split(
        total_cents=agreed_price_cents,
        platform_fee_cents=fee,
        cleaner_cents=agreed_price_cents - fee,
    )


def reconcile(payment: PaymentIn, payout: Payout | None) -> dict[str, int]:
    """Collected, minus what was refunded, equals paid out plus the fee kept.

    The carry-over check from CLAUDE.md, written as a function rather than a
    note so a test can assert it and a future ledger screen can read it. A
    non-zero `drift` means something moved that this code did not account for —
    the number to alarm on, not to paper over.
    """
    collected = payment.amount_cents - payment.refunded_amount_cents
    paid_out = 0
    if payout is not None:
        paid_out = payout.amount_cents - payout.reversed_amount_cents

    fee_kept = payment.platform_fee_cents
    if payment.refunded_amount_cents:
        # A full refund gives the fee back too (see refund_payment). Partial
        # refunds are not offered at v1, so this is all-or-nothing on purpose.
        fee_kept = 0

    return {
        "collected_cents": collected,
        "paid_out_cents": paid_out,
        "platform_fee_cents": fee_kept,
        "drift_cents": collected - paid_out - fee_kept,
    }


# --------------------------------------------------------------------------
# Connect onboarding — Express, so identity and 1099s belong to Stripe
# --------------------------------------------------------------------------


#: Where the app itself lives, for the URLs Stripe sends people back to. In
#: development the frontend is on its own dev server; in production the single
#: container serves both from one origin.
def _app_base() -> str:
    return (settings.public_base_url or "http://localhost:5173").rstrip("/")


def _return_urls(path: str) -> tuple[str, str]:
    """Refresh and return URLs for a hosted Stripe flow.

    Both point at a real route in this app. A return URL that 404s strands the
    cleaner at the end of onboarding with no way to tell whether it worked,
    which is the one moment they most need to be told.
    """
    base = _app_base()
    return f"{base}{path}?stripe=refresh", f"{base}{path}?stripe=return"


def ensure_connected_account(db: Session, *, user: User, profile: CleanerProfile) -> str:
    """The cleaner's Express account, created once and remembered.

    **Express, not Custom.** Identity verification and 1099 reporting sit with
    Stripe, the same reasoning as Checkr collecting the SSN: regulated data we
    never hold is regulated data we cannot lose.
    """
    if profile.stripe_account_id:
        return profile.stripe_account_id

    account = stripe_client.post(
        "/accounts",
        {
            "type": "express",
            "email": user.email,
            "capabilities": {"transfers": {"requested": True}},
            "business_type": "individual",
            "metadata": {"cleaner_profile_id": str(profile.id), "user_id": str(user.id)},
        },
        # Derived from the profile, so a double-click cannot leave one cleaner
        # holding two connected accounts with the money going to whichever one
        # a later call happens to read.
        idempotency_key=f"connect:account:{profile.id}",
    )

    profile.stripe_account_id = account["id"]
    db.commit()
    return profile.stripe_account_id


def onboarding_link(db: Session, *, user: User, profile: CleanerProfile) -> str:
    """A one-time URL into Stripe's hosted Express onboarding."""
    account_id = ensure_connected_account(db, user=user, profile=profile)
    refresh_url, return_url = _return_urls("/cleaner/profile")

    link = stripe_client.post(
        "/account_links",
        {
            "account": account_id,
            "refresh_url": refresh_url,
            "return_url": return_url,
            "type": "account_onboarding",
        },
        # The one signed exception in the codebase. An account link is a
        # short-lived URL that moves no money and creates nothing durable;
        # reusing a key here would hand the cleaner back an expired link and
        # strand them, which is the opposite of what the key is for.
        non_idempotent_reason=(
            "account links expire and must be re-issued; they move no money "
            "and create no duplicate object"
        ),
    )
    return link["url"]


def refresh_connect_status(db: Session, profile: CleanerProfile) -> CleanerProfile:
    """Ask Stripe whether this account can actually receive money.

    Never inferred from "they finished the form". Submitting details and
    passing verification are different events, and treating the first as the
    second is how a payment fails at the moment it matters.
    """
    if not profile.stripe_account_id:
        return profile

    account = stripe_client.get(f"/accounts/{profile.stripe_account_id}")
    profile.stripe_payouts_enabled = bool(account.get("payouts_enabled"))
    profile.stripe_details_submitted = bool(account.get("details_submitted"))
    db.commit()
    return profile


def payout_blocker(profile: CleanerProfile | None) -> str | None:
    """Why this cleaner cannot be paid yet, in words, or None if they can.

    Deliberately separate from `can_take_jobs`: the trust gate answers "may
    this person be in a stranger's house", and this answers "can money reach
    them". One place each, and neither pretends to be the other.
    """
    if profile is None or not profile.stripe_account_id:
        return "The cleaner has not set up payouts yet."
    if not profile.stripe_payouts_enabled:
        if profile.stripe_details_submitted:
            return "Stripe is still verifying the cleaner's payout account."
        return "The cleaner has started but not finished payout setup."
    return None


# --------------------------------------------------------------------------
# Collecting — one destination charge, through hosted Checkout
# --------------------------------------------------------------------------


def _existing_payment(db: Session, turnover_id: uuid.UUID) -> PaymentIn | None:
    return db.execute(
        select(PaymentIn).where(PaymentIn.turnover_id == turnover_id)
    ).scalar_one_or_none()


def start_checkout(
    db: Session,
    *,
    turnover: Turnover,
    prop: Property,
    award: Award,
    owner: User,
) -> str:
    """Raise the charge for a finished job and hand back a Stripe URL.

    **The turnover row must already be locked.** Same shape as accepting a bid
    and for the same reason: two tabs both reading "not paid yet" and both
    charging is the failure this prevents, and the unique index on
    `payments_in.turnover_id` is the backstop, not the mechanism.
    """
    if not stripe_client.is_configured():
        raise PaymentRefused(
            "Payments are not configured on this deployment yet."
        )
    if award.completed_at is None:
        raise PaymentRefused(
            "This job has not been marked complete. Payment is raised for work "
            "that happened, not for a booking."
        )

    cleaner_profile = db.execute(
        select(CleanerProfile).where(CleanerProfile.user_id == award.cleaner_id)
    ).scalar_one_or_none()
    blocker = payout_blocker(cleaner_profile)
    if blocker:
        raise PaymentRefused(blocker)

    payment = _existing_payment(db, turnover.id)
    if payment and payment.status is PaymentStatus.SUCCEEDED:
        raise PaymentRefused("This turnover has already been paid.")
    if payment and payment.status is PaymentStatus.REQUIRES_REVIEW:
        raise PaymentRefused(
            "A previous payment attempt on this turnover has an unknown "
            "outcome and is waiting on a human. It will not be retried "
            "automatically."
        )

    split = split_for(award.agreed_price_cents)

    if payment is None:
        payment = PaymentIn(
            turnover_id=turnover.id,
            amount_cents=split.total_cents,
            platform_fee_cents=split.platform_fee_cents,
            status=PaymentStatus.PENDING,
            # Guardrail 2: derived from the award, never generated. A retry
            # produces this same string and Stripe recognizes it as a retry.
            idempotency_key=f"charge:award:{award.id}",
        )
        db.add(payment)

    # Guardrail 2, the other half: the attempt is on the row and committed
    # before the network call. A crash between here and the response leaves a
    # row that visibly tried, rather than one that looks untouched.
    payment.attempted_at = datetime.now(timezone.utc)
    payment.status = PaymentStatus.PROCESSING
    payment.failure_message = None
    db.commit()

    success_url = f"{_app_base()}/turnovers/{turnover.id}?paid=1"
    cancel_url = f"{_app_base()}/turnovers/{turnover.id}?paid=0"

    try:
        session = stripe_client.post(
            "/checkout/sessions",
            {
                "mode": "payment",
                "success_url": success_url,
                "cancel_url": cancel_url,
                "customer_email": owner.email,
                "client_reference_id": str(turnover.id),
                "line_items": [
                    {
                        "quantity": 1,
                        "price_data": {
                            "currency": "usd",
                            "unit_amount": split.total_cents,
                            "product_data": {
                                "name": f"Turnover cleaning — {prop.nickname}",
                                "description": (
                                    f"{prop.city}, {prop.state} · "
                                    f"checkout {turnover.checkout_at:%a %d %b}"
                                ),
                            },
                        },
                    }
                ],
                "payment_intent_data": {
                    # The destination charge. Stripe moves
                    # `total - application_fee` to the cleaner as part of this
                    # one transaction; there is no second call to forget.
                    "application_fee_amount": split.platform_fee_cents,
                    "transfer_data": {"destination": cleaner_profile.stripe_account_id},
                    "metadata": {
                        "turnover_id": str(turnover.id),
                        "award_id": str(award.id),
                        "cleaner_id": str(award.cleaner_id),
                    },
                },
                "metadata": {"turnover_id": str(turnover.id), "award_id": str(award.id)},
            },
            idempotency_key=payment.idempotency_key,
        )
    except stripe_client.StripeError as exc:
        # An unknown outcome is never assumed failed either. A refused request
        # (Stripe answered) is a failure; an unreachable Stripe is not.
        payment.status = (
            PaymentStatus.FAILED if exc.status else PaymentStatus.REQUIRES_REVIEW
        )
        payment.failure_message = str(exc)
        db.commit()
        raise PaymentRefused(str(exc)) from exc

    payment.stripe_checkout_session_id = session["id"]
    if session.get("payment_intent"):
        payment.stripe_payment_intent_id = session["payment_intent"]
    db.commit()

    return session["url"]


def lookup_transfer(payment_intent_id: str | None) -> str | None:
    """Find the transfer Stripe created as part of the destination charge.

    A `checkout.session.completed` event does not carry it — the transfer hangs
    off the charge, one level down from the PaymentIntent — so it is read back
    rather than guessed at from the session. Worth the extra call: without it
    the payout row has no Stripe reference, and a refund that needs to reverse
    the transfer has nothing to point at when somebody is arguing about money.

    A failure here is logged and swallowed on purpose. The money has already
    moved; losing the reference is bad, and failing the webhook so Stripe
    retries forever is worse.
    """
    if not payment_intent_id:
        return None
    try:
        intent = stripe_client.get(
            f"/payment_intents/{payment_intent_id}?expand[]=latest_charge"
        )
    except stripe_client.StripeError as exc:
        logger.warning("could not read the transfer for %s: %s", payment_intent_id, exc)
        return None

    charge = intent.get("latest_charge")
    if isinstance(charge, dict):
        return charge.get("transfer")
    return None


def settle(
    db: Session,
    *,
    payment: PaymentIn,
    payment_intent_id: str | None,
    transfer_id: str | None,
) -> Payout | None:
    """Record that the money actually moved. Called from the webhook.

    Creates the `Payout` row from the transfer Stripe made as part of the
    destination charge — a record of something that happened, not an
    instruction to do something.
    """
    if payment.status is PaymentStatus.SUCCEEDED:
        # A redelivered webhook. Stripe retries; that is normal, and doing this
        # twice must be a no-op rather than a second payout row.
        return db.execute(
            select(Payout).where(Payout.turnover_id == payment.turnover_id)
        ).scalar_one_or_none()

    if payment_intent_id:
        payment.stripe_payment_intent_id = payment_intent_id
    payment.status = PaymentStatus.SUCCEEDED
    payment.failure_message = None

    # The cleaner's share is the remainder of what was actually collected,
    # read off the stored fee rather than re-derived from the rate. If the rate
    # changes between the charge and the webhook, the money that moved is what
    # the charge said, not what the setting says now.
    cleaner_cents = payment.amount_cents - payment.platform_fee_cents

    award = db.execute(
        select(Award).where(
            Award.turnover_id == payment.turnover_id, Award.cancelled_at.is_(None)
        )
    ).scalar_one_or_none()

    payout = db.execute(
        select(Payout).where(Payout.turnover_id == payment.turnover_id)
    ).scalar_one_or_none()
    if payout is None and award is not None:
        payout = Payout(
            cleaner_id=award.cleaner_id,
            turnover_id=payment.turnover_id,
            payment_in_id=payment.id,
            amount_cents=cleaner_cents,
            status=PaymentStatus.SUCCEEDED,
            stripe_transfer_id=transfer_id,
            # Same derivation rule as the charge. Nothing here generates a key.
            idempotency_key=f"payout:award:{award.id}",
            attempted_at=datetime.now(timezone.utc),
        )
        db.add(payout)
    elif payout is not None:
        payout.status = PaymentStatus.SUCCEEDED
        payout.stripe_transfer_id = transfer_id or payout.stripe_transfer_id

    return payout


def mark_failed(db: Session, *, payment: PaymentIn, reason: str) -> None:
    """Stripe said the charge did not go through. Said out loud, not swallowed."""
    if payment.status is PaymentStatus.SUCCEEDED:
        # Out-of-order delivery. A success already recorded is not undone by a
        # late failure notice for an earlier attempt.
        logger.warning(
            "ignoring a failure notice for payment %s, already succeeded", payment.id
        )
        return
    payment.status = PaymentStatus.FAILED
    payment.failure_message = reason


# --------------------------------------------------------------------------
# Refunds — their own path, decided rather than reversed
# --------------------------------------------------------------------------


def refund_payment(db: Session, *, payment: PaymentIn, reason: str) -> PaymentIn:
    """Give the owner their money back, and unwind both halves of the split.

    Refunding a destination charge is **not** a simple reversal, and writing it
    as one is how money ends up stranded. Two decisions have to be made
    explicitly, and this is where they are made:

    * **The application fee comes back too** (`refund_application_fee`). We did
      not earn a cut of a cleaning the owner is being refunded for.
    * **The transfer to the cleaner is reversed** (`reverse_transfer`). Without
      this the owner is made whole out of the platform's own balance while the
      cleaner keeps the full amount — the money is not "lost", it is silently
      ours to eat, which is worse because nothing errors.

    Full refunds only at v1. A partial refund has to decide how to split the
    shortfall between the fee and the cleaner, and that is a policy question
    with a person's income on the other end of it — it gets designed when it is
    needed, not guessed at here.
    """
    if payment.status is not PaymentStatus.SUCCEEDED:
        raise PaymentRefused("Only a completed payment can be refunded.")
    if payment.refunded_amount_cents:
        raise PaymentRefused("This payment has already been refunded.")
    if not payment.stripe_payment_intent_id:
        raise PaymentRefused(
            "This payment has no Stripe reference to refund against; it needs "
            "a human."
        )

    payment.attempted_at = datetime.now(timezone.utc)
    db.commit()

    try:
        stripe_client.post(
            "/refunds",
            {
                "payment_intent": payment.stripe_payment_intent_id,
                "refund_application_fee": True,
                "reverse_transfer": True,
                "metadata": {"turnover_id": str(payment.turnover_id), "reason": reason},
            },
            idempotency_key=f"refund:payment:{payment.id}",
        )
    except stripe_client.StripeError as exc:
        payment.status = (
            PaymentStatus.FAILED if exc.status else PaymentStatus.REQUIRES_REVIEW
        )
        payment.failure_message = f"refund failed: {exc}"
        db.commit()
        raise PaymentRefused(str(exc)) from exc

    payment.refunded_amount_cents = payment.amount_cents
    payment.status = PaymentStatus.REFUNDED
    payment.failure_message = reason

    payout = db.execute(
        select(Payout).where(Payout.turnover_id == payment.turnover_id)
    ).scalar_one_or_none()
    if payout is not None:
        # The row is kept and marked, never deleted — what was paid and then
        # clawed back is the history a dispute is argued from, the same reason
        # a cancelled award is cancelled rather than deleted.
        payout.reversed_amount_cents = payout.amount_cents
        payout.status = PaymentStatus.REFUNDED

    db.commit()
    return payment


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def payment_for(db: Session, turnover_id: uuid.UUID) -> PaymentIn | None:
    return _existing_payment(db, turnover_id)


def payout_for(db: Session, turnover_id: uuid.UUID) -> Payout | None:
    return db.execute(
        select(Payout).where(Payout.turnover_id == turnover_id)
    ).scalar_one_or_none()


def announce_settlement(
    db: Session, *, payment: PaymentIn, payout: Payout | None
) -> None:
    """Tell both sides the money moved.

    Two entries from the fixed list that have been declared and unwired since
    phase 5: the owner's receipt and the cleaner's payout notice. Queued inside
    the caller's transaction like every other notification, delivered after it.
    """
    turnover = db.get(Turnover, payment.turnover_id)
    if turnover is None:
        return
    prop = db.get(Property, turnover.property_id)
    if prop is None:
        return

    notifications.payment_receipt(db, turnover, prop, payment)
    if payout is not None:
        notifications.payout_notice(db, turnover, prop, payout)


def turnover_is_payable(turnover: Turnover, award: Award | None) -> bool:
    """Whether the owner should be shown a pay button at all."""
    return (
        award is not None
        and award.cancelled_at is None
        and award.completed_at is not None
        and turnover.status is TurnoverStatus.COMPLETED
    )
