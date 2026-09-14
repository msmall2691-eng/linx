"""Money endpoints: paying for a finished job, getting set up to be paid.

Three audiences, three shapes, and one rule they all share: **nothing here
talks to Stripe directly.** Every call goes through `app/services/payments.py`,
which goes through `app/services/stripe_client.py`. A route that reached for
the Stripe API itself would be the second door guardrail 2 exists to prevent.

The webhook is the only endpoint in the product with no authentication, and it
compensates with a signature check that refuses everything when no secret is
configured. An unsigned POST that says a payment succeeded is a POST from
anyone.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require_role
from app.db import get_db
from app.models.cleaner_profile import CleanerProfile
from app.models.enums import UserRole
from app.models.payment import PaymentIn
from app.models.property import Property
from app.models.turnover import Turnover
from app.models.user import User
from app.schemas.payment import (
    ConnectStatusOut,
    OnboardingLinkOut,
    PaymentOut,
    RefundRequest,
)
from app.services import awards, notifications, payments, stripe_client

logger = logging.getLogger("linx.payments.api")

router = APIRouter(tags=["payments"])


# --------------------------------------------------------------------------
# The cleaner's side — getting set up to receive money
# --------------------------------------------------------------------------


def _my_profile(db: Session, user: User) -> CleanerProfile:
    profile = db.execute(
        select(CleanerProfile).where(CleanerProfile.user_id == user.id)
    ).scalar_one_or_none()
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Set up your cleaner profile first.",
        )
    return profile


def _connect_status(profile: CleanerProfile) -> ConnectStatusOut:
    return ConnectStatusOut(
        connected=bool(profile.stripe_account_id),
        details_submitted=profile.stripe_details_submitted,
        payouts_enabled=profile.stripe_payouts_enabled,
        blocker=payments.payout_blocker(profile),
        payments_configured=stripe_client.is_configured(),
    )


@router.get("/payouts/status", response_model=ConnectStatusOut)
def read_payout_status(
    refresh: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(UserRole.CLEANER)),
) -> ConnectStatusOut:
    """Can money reach this cleaner yet, and if not, what is missing.

    `refresh=true` asks Stripe rather than reading our copy. Whether the account
    passed verification is Stripe's answer, never one we infer from the cleaner
    having finished the form.
    """
    profile = _my_profile(db, user)
    if refresh and profile.stripe_account_id and stripe_client.is_configured():
        try:
            payments.refresh_connect_status(db, profile)
        except stripe_client.StripeError as exc:
            # A status read failing is not a reason to fail the page. Answer
            # with what we last knew, rather than a blank screen.
            logger.warning("could not refresh Connect status for %s: %s", profile.id, exc)

    return _connect_status(profile)


@router.post("/payouts/onboarding", response_model=OnboardingLinkOut)
def create_onboarding_link(
    db: Session = Depends(get_db),
    user: User = Depends(require_role(UserRole.CLEANER)),
) -> OnboardingLinkOut:
    """A link into Stripe's hosted Express onboarding.

    Express rather than Custom, so identity verification and 1099 reporting
    stay with Stripe — the same reasoning as Checkr collecting the SSN.
    """
    profile = _my_profile(db, user)
    try:
        url = payments.onboarding_link(db, user=user, profile=profile)
    except stripe_client.StripeNotConfigured as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from None
    except stripe_client.StripeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from None

    return OnboardingLinkOut(url=url)


# --------------------------------------------------------------------------
# The owner's side — paying for a finished job
# --------------------------------------------------------------------------


def _payment_out(
    db: Session, turnover_id: uuid.UUID, payment: PaymentIn | None
) -> PaymentOut:
    payout = payments.payout_for(db, turnover_id)
    if payment is None:
        return PaymentOut(
            turnover_id=turnover_id,
            status=None,
            amount_cents=None,
            platform_fee_cents=None,
            cleaner_cents=None,
            refunded_amount_cents=0,
            paid_out_cents=payout.amount_cents if payout else None,
            failure_message=None,
        )

    return PaymentOut(
        turnover_id=turnover_id,
        status=payment.status,
        amount_cents=payment.amount_cents,
        platform_fee_cents=payment.platform_fee_cents,
        cleaner_cents=payment.amount_cents - payment.platform_fee_cents,
        refunded_amount_cents=payment.refunded_amount_cents,
        paid_out_cents=(payout.amount_cents - payout.reversed_amount_cents)
        if payout
        else None,
        failure_message=payment.failure_message,
    )


def _owned_turnover(db: Session, turnover_id: uuid.UUID, owner: User) -> Turnover:
    """Locked first, then checked — the same order as every other action here."""
    turnover = awards.lock_turnover(db, turnover_id)
    if turnover is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Turnover not found"
        )
    prop = db.get(Property, turnover.property_id)
    if prop is None or prop.owner_id != owner.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Turnover not found"
        )
    return turnover


@router.get("/turnovers/{turnover_id}/payment", response_model=PaymentOut)
def read_payment(
    turnover_id: uuid.UUID,
    db: Session = Depends(get_db),
    owner: User = Depends(require_role(UserRole.OWNER)),
) -> PaymentOut:
    """What has been charged for this turnover, if anything."""
    prop_owned = db.execute(
        select(Turnover)
        .join(Property, Turnover.property_id == Property.id)
        .where(Turnover.id == turnover_id, Property.owner_id == owner.id)
    ).scalar_one_or_none()
    if prop_owned is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Turnover not found"
        )

    return _payment_out(db, turnover_id, payments.payment_for(db, turnover_id))


@router.post("/turnovers/{turnover_id}/pay")
def pay_for_turnover(
    turnover_id: uuid.UUID,
    db: Session = Depends(get_db),
    owner: User = Depends(require_role(UserRole.OWNER)),
) -> dict[str, str]:
    """Raise the charge and hand back Stripe's hosted payment page.

    The turnover row is locked before anything is read about it, so two tabs
    cannot both decide nothing has been charged yet. The unique index on
    `payments_in.turnover_id` is the backstop behind that, not the mechanism.

    Card details go to Stripe's page, never to this server.
    """
    turnover = _owned_turnover(db, turnover_id, owner)
    award = awards.live_award(db, turnover.id)
    if award is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Nobody is booked for this turnover.",
        )

    prop = db.execute(
        select(Property).where(Property.id == turnover.property_id)
    ).scalar_one()

    try:
        url = payments.start_checkout(
            db, turnover=turnover, prop=prop, award=award, owner=owner
        )
    except payments.PaymentRefused as refused:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=refused.detail
        ) from None

    return {"checkout_url": url}


@router.post("/turnovers/{turnover_id}/refund", response_model=PaymentOut)
def refund_turnover(
    turnover_id: uuid.UUID,
    payload: RefundRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(require_role(UserRole.ADMIN)),
) -> PaymentOut:
    """Give the owner their money back. **Admin only, at v1.**

    A refund reverses a transfer somebody has already been told they earned, so
    it is a human decision made in the dispute inbox rather than a button on the
    owner's screen. Disputes go to a person, not a bot (CLAUDE.md), and this is
    the money half of that rule.
    """
    payment = payments.payment_for(db, turnover_id)
    if payment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No payment on this turnover"
        )

    try:
        payments.refund_payment(db, payment=payment, reason=payload.reason)
    except payments.PaymentRefused as refused:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=refused.detail
        ) from None

    logger.info("refund on turnover %s issued by admin %s", turnover_id, admin.id)
    return _payment_out(db, turnover_id, payment)


# --------------------------------------------------------------------------
# The webhook — the only unauthenticated endpoint, and the only signed one
# --------------------------------------------------------------------------


@router.post("/stripe/webhook", include_in_schema=False)
async def stripe_webhook(request: Request, db: Session = Depends(get_db)) -> Response:
    """Stripe telling us what actually happened.

    **This is where a payment becomes true.** The request that starts a checkout
    only creates an intent to pay; the money moving is something Stripe reports
    afterwards, and believing the first as though it were the second is how a
    receipt goes out for a charge that failed.

    Redeliveries are normal — Stripe retries until it gets a 2xx — so every
    handler here is written to be a no-op the second time.
    """
    payload = await request.body()
    try:
        event = stripe_client.verify_webhook(
            payload, request.headers.get("Stripe-Signature")
        )
    except stripe_client.WebhookVerificationError as exc:
        logger.warning("refused a Stripe webhook delivery: %s", exc)
        # 400, not 401: Stripe reads this as "do not retry, it will not get
        # better", which is true — a bad signature will still be bad in an hour.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="invalid signature"
        ) from None

    kind = event.get("type") or ""
    obj = (event.get("data") or {}).get("object") or {}
    logger.info("stripe webhook: %s", kind)

    if kind == "checkout.session.completed":
        _handle_checkout_completed(db, obj)
    elif kind in {"payment_intent.payment_failed", "checkout.session.async_payment_failed"}:
        _handle_payment_failed(db, obj)
    elif kind == "account.updated":
        _handle_account_updated(db, obj)
    # Anything else is acknowledged and ignored. An unrecognised event is not
    # an error, and 500ing on one would have Stripe retry it forever.

    return Response(status_code=status.HTTP_200_OK)


def _payment_from_event(db: Session, obj: dict) -> PaymentIn | None:
    """Find our row from whatever identifier the event carries."""
    session_id = obj.get("id") if obj.get("object") == "checkout.session" else None
    intent_id = (
        obj.get("payment_intent")
        if obj.get("object") == "checkout.session"
        else obj.get("id")
    )
    turnover_id = (obj.get("metadata") or {}).get("turnover_id")

    stmt = None
    if session_id:
        stmt = select(PaymentIn).where(PaymentIn.stripe_checkout_session_id == session_id)
        found = db.execute(stmt).scalar_one_or_none()
        if found:
            return found
    if intent_id:
        found = db.execute(
            select(PaymentIn).where(PaymentIn.stripe_payment_intent_id == intent_id)
        ).scalar_one_or_none()
        if found:
            return found
    if turnover_id:
        try:
            return payments.payment_for(db, uuid.UUID(turnover_id))
        except ValueError:
            return None
    return None


def _handle_checkout_completed(db: Session, obj: dict) -> None:
    payment = _payment_from_event(db, obj)
    if payment is None:
        # Not ours, or ours and unrecognisable. Loud, because a payment nobody
        # can match to a turnover is money in limbo, not a non-event.
        logger.error("checkout.session.completed with no matching payment row: %s", obj.get("id"))
        return

    if (obj.get("payment_status") or "") not in {"paid", "no_payment_required"}:
        logger.info("checkout session %s completed unpaid; leaving it alone", obj.get("id"))
        return

    intent_id = obj.get("payment_intent")
    payout = payments.settle(
        db,
        payment=payment,
        payment_intent_id=intent_id,
        # Read back rather than taken off the session, which does not carry it.
        transfer_id=payments.lookup_transfer(intent_id),
    )
    # Queued inside the same transaction as the state change, delivered after
    # the commit — the phase 5 contract, unchanged.
    payments.announce_settlement(db, payment=payment, payout=payout)
    db.commit()
    notifications.deliver_pending(db)


def _handle_payment_failed(db: Session, obj: dict) -> None:
    payment = _payment_from_event(db, obj)
    if payment is None:
        return
    reason = (
        (obj.get("last_payment_error") or {}).get("message")
        or "Stripe reported the payment failed."
    )
    payments.mark_failed(db, payment=payment, reason=reason)
    db.commit()


def _handle_account_updated(db: Session, obj: dict) -> None:
    account_id = obj.get("id")
    if not account_id:
        return
    profile = db.execute(
        select(CleanerProfile).where(CleanerProfile.stripe_account_id == account_id)
    ).scalar_one_or_none()
    if profile is None:
        return

    profile.stripe_payouts_enabled = bool(obj.get("payouts_enabled"))
    profile.stripe_details_submitted = bool(obj.get("details_submitted"))
    db.commit()
