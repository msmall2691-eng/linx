"""The admin console — the dispute inbox, the unclaimed alarm, and the ledger.

Phase 8. Three of these four things already existed as data nobody could see: a
`reconcile()` function with no route, an unclaimed alarm that emailed an admin
and then had nowhere to send them, and a dispute policy with no table. The
vetting queue is the exception and stays exactly where it is —
`app/api/routes/admin.py`, working and tested since phase 3. This router sits
beside it under the same prefix rather than absorbing it, because the trust gate
is the last thing worth destabilising for tidiness.

**Read-only, with one exception.** The console shows what needs a person and
lets that person resolve a dispute. It does not refund: there is exactly one
door to Stripe, and the refund endpoint is already on the payments router where
the rest of the money lives. A second one here would be a second place that
moves money, which is how one of them ends up missing an idempotency key.

Nothing in here re-derives a rule. Unclaimed comes from
`turnovers.unclaimed_alarming` — the same function the scheduled alarm uses, so
the screen and the email cannot disagree. Drift comes from
`payments.reconcile`. Clearance comes from `vetting`.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import require_role
from app.db import get_db
from app.models.award import Award
from app.models.bid import Bid
from app.models.cleaner_profile import CleanerProfile
from app.models.dispute import Dispute
from app.models.enums import (
    BidStatus,
    UserRole,
    VerificationStatus,
)
from app.models.payment import PaymentIn
from app.models.property import Property
from app.models.turnover import Turnover
from app.models.user import User
from app.schemas.console import (
    ConsoleSummaryOut,
    LedgerOut,
    LedgerRowOut,
    UnclaimedTurnoverOut,
)
from app.schemas.dispute import AdminDisputeOut, DisputePartyOut, DisputeResolution
from app.services import disputes, payments, turnovers
from app.services.turnovers import refresh_urgency

router = APIRouter(prefix="/admin", tags=["admin"])

require_admin = require_role(UserRole.ADMIN)


# --------------------------------------------------------------------------
# The summary — what needs a person
# --------------------------------------------------------------------------


def _vetting_pending(db: Session) -> int:
    """Cleaners with a step still waiting on a human.

    `PENDING` only. `NOT_STARTED` is a cleaner who has not sent anything yet —
    counting them would put the admin's queue at the mercy of how many people
    signed up, and an inbox whose number never reaches zero is an inbox nobody
    reads.
    """
    return len(
        db.execute(
            select(CleanerProfile.id).where(
                (CleanerProfile.id_verification_status == VerificationStatus.PENDING)
                | (CleanerProfile.background_check_status == VerificationStatus.PENDING)
            )
        )
        .scalars()
        .all()
    )


@router.get("/summary", response_model=ConsoleSummaryOut)
def read_summary(
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> ConsoleSummaryOut:
    """Four numbers, so an admin can tell in one glance whether to stop."""
    del admin  # the role gate is the authorization; the identity is not used

    drifting = sum(
        1 for row in _ledger_rows(db) if row.drift_cents != 0
    )

    return ConsoleSummaryOut(
        open_disputes=disputes.open_count(db),
        vetting_pending=_vetting_pending(db),
        unclaimed_turnovers=len(turnovers.unclaimed_alarming(db)),
        payments_with_drift=drifting,
    )


# --------------------------------------------------------------------------
# The dispute inbox
# --------------------------------------------------------------------------


def _party(user: User) -> DisputePartyOut:
    return DisputePartyOut.model_validate(user)


def _admin_dispute(db: Session, dispute: Dispute) -> AdminDisputeOut | None:
    """A dispute with both sides attached, for somebody who has to settle it.

    Returns None if the turnover or its award has gone — a dispute whose job no
    longer exists cannot be worked, and the inbox skips it rather than rendering
    a row with holes in it.
    """
    turnover = db.get(Turnover, dispute.turnover_id)
    if turnover is None:
        return None
    # **The award this dispute was filed against**, not whoever holds the job
    # now. This screen is the one place in the product that shows the owner's
    # identity and both sides' phone numbers; reading the turnover's latest
    # award here put a replacement cleaner's contact details on somebody else's
    # complaint.
    people = disputes.parties_of_dispute(db, dispute)
    prop = db.get(Property, turnover.property_id)
    if people is None or prop is None:
        return None

    raiser = db.get(User, dispute.raised_by_id)
    if raiser is None:
        return None

    return AdminDisputeOut(
        id=dispute.id,
        turnover_id=dispute.turnover_id,
        raised_by_role=dispute.raised_by_role,
        reason=dispute.reason,
        description=dispute.description,
        status=dispute.status,
        acknowledged_at=dispute.acknowledged_at,
        resolved_at=dispute.resolved_at,
        resolution_notes=dispute.resolution_notes,
        created_at=dispute.created_at,
        updated_at=dispute.updated_at,
        property_nickname=prop.nickname,
        property_city=prop.city,
        property_state=prop.state,
        checkout_at=turnover.checkout_at,
        owner=_party(people.owner),
        cleaner=_party(people.cleaner),
        raised_by=_party(raiser),
        award_cancelled_at=people.award.cancelled_at,
        award_was_no_show=bool(people.award.was_no_show),
    )


def _load_dispute(db: Session, dispute_id: uuid.UUID) -> Dispute:
    dispute = db.get(Dispute, dispute_id)
    if dispute is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Dispute not found"
        )
    return dispute


def _answer_dispute(db: Session, dispute: Dispute) -> AdminDisputeOut:
    answer = _admin_dispute(db, dispute)
    if answer is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Dispute not found"
        )
    return answer


@router.get("/disputes", response_model=list[AdminDisputeOut])
def dispute_inbox(
    include_resolved: bool = Query(
        default=False, description="Include disputes that have been settled."
    ),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[AdminDisputeOut]:
    """The queue. Open first, oldest first.

    Newest-first is the wrong default for a queue somebody works: it buries the
    complaint that has waited longest under the one that arrived this morning,
    and the one that has waited longest is the one about to become a phone call.
    """
    del admin
    rows = [
        _admin_dispute(db, dispute)
        for dispute in disputes.inbox(
            db, include_resolved=include_resolved, limit=limit, offset=offset
        )
    ]
    return [row for row in rows if row is not None]


@router.get("/disputes/{dispute_id}", response_model=AdminDisputeOut)
def read_dispute(
    dispute_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> AdminDisputeOut:
    del admin
    return _answer_dispute(db, _load_dispute(db, dispute_id))


@router.post("/disputes/{dispute_id}/acknowledge", response_model=AdminDisputeOut)
def acknowledge_dispute(
    dispute_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> AdminDisputeOut:
    """Mark that a person has picked this up. Sends nothing.

    Answers with the whole dispute — the same shape the GET gave — so the
    screen keeps everything it was showing.
    """
    dispute = _load_dispute(db, dispute_id)
    try:
        disputes.acknowledge(db, dispute, admin)
    except disputes.DisputeRefused as refused:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=refused.detail
        ) from None
    return _answer_dispute(db, dispute)


@router.post("/disputes/{dispute_id}/resolve", response_model=AdminDisputeOut)
def resolve_dispute(
    dispute_id: uuid.UUID,
    payload: DisputeResolution,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> AdminDisputeOut:
    """Settle it, and tell both sides what was decided.

    The note is required and is sent verbatim. **Resolving refunds nothing** —
    if money should move, that is the refund endpoint, deliberately a separate
    decision with its own reason, because "the dispute is closed" and "the owner
    got their money back" are not the same sentence and must not be one click.
    """
    dispute = _load_dispute(db, dispute_id)
    try:
        disputes.resolve(db, dispute, admin, payload.notes)
    except disputes.DisputeRefused as refused:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=refused.detail
        ) from None
    return _answer_dispute(db, dispute)


# --------------------------------------------------------------------------
# The unclaimed alarm, as a screen
# --------------------------------------------------------------------------


@router.get("/unclaimed", response_model=list[UnclaimedTurnoverOut])
def unclaimed(
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[UnclaimedTurnoverOut]:
    """Jobs nobody has taken, with checkout coming up.

    Reads `turnovers.unclaimed_alarming` — the same function behind the email an
    admin was already being sent. Two definitions of "unclaimed" would mean a
    screen that disagrees with the alert, and no way to tell which is lying.
    """
    del admin

    rows = turnovers.unclaimed_alarming(db)
    if not rows:
        return []

    # **The one screen that must not show a stale rung.** A standing vacancy's
    # urgency is measured against *now*, so it climbs as checkout approaches —
    # and a turnover nobody has claimed is very often exactly that, because a
    # cancellation re-posts it with `reopened_at` set. Read straight from the
    # column, a job created days ago and never opened on the board since still
    # says `standard` while checkout is hours away, on the queue whose whole
    # purpose is to sort out what is most urgent. Every other read path calls
    # this; the newest one was the only one that did not.
    refresh_urgency(db, [turnover for turnover, _ in rows])

    turnover_ids = [turnover.id for turnover, _ in rows]
    # **Only bids the owner could actually accept right now.** Counting every
    # historical row made the alarm lie in the direction that matters: a job
    # re-posted after a cancellation still carries the accepted bid and every
    # declined one, so the console read "3 bids, none accepted" — an owner
    # dithering over offers — when there were no live offers at all and the
    # real problem was that nobody had bid. An operational alarm that
    # misdescribes the problem is worse than one that does not fire.
    counts = dict(
        db.execute(
            select(Bid.turnover_id, func.count(Bid.id))
            .where(
                Bid.turnover_id.in_(turnover_ids),
                Bid.status == BidStatus.SUBMITTED,
            )
            .group_by(Bid.turnover_id)
        ).all()
    )

    out: list[UnclaimedTurnoverOut] = []
    for turnover, prop in rows:
        owner = db.get(User, prop.owner_id)
        if owner is None:
            continue
        out.append(
            UnclaimedTurnoverOut(
                turnover_id=turnover.id,
                property_nickname=prop.nickname,
                property_city=prop.city,
                property_state=prop.state,
                checkout_at=turnover.checkout_at,
                checkin_at=turnover.checkin_at,
                urgency=turnover.urgency,
                owner_budget_cents=turnover.owner_budget_cents,
                # Zero bids and "four bids, none accepted" are different
                # problems: a supply gap, or an owner who has not chosen.
                bid_count=int(counts.get(turnover.id, 0)),
                owner_name=owner.full_name,
                owner_email=owner.email,
            )
        )
    return out


# --------------------------------------------------------------------------
# The ledger
# --------------------------------------------------------------------------


def _ledger_rows(db: Session) -> list[LedgerRowOut]:
    """Every payment, with its reconciliation worked out by `payments.reconcile`.

    The drift column is the whole reason this screen exists. `reconcile()` has
    been able to answer it since phase 6 and nothing has ever asked — a number
    nobody reads is a number that can be wrong for a year.
    """
    rows = db.execute(
        select(PaymentIn, Turnover, Property)
        .join(Turnover, Turnover.id == PaymentIn.turnover_id)
        .join(Property, Property.id == Turnover.property_id)
        .order_by(PaymentIn.created_at.desc())
    ).all()

    out: list[LedgerRowOut] = []
    for payment, turnover, prop in rows:
        payout = payments.payout_for(db, turnover.id)
        # **Status-aware, because `reconcile` is settled-payment arithmetic.**
        # An ordinary checkout waiting on its webhook has an amount and no
        # payout, so reconciling it reported the whole cleaner share as drift —
        # the console's financial alarm going off for every payment in flight,
        # which is how an alarm becomes something people scroll past. Worse, it
        # claimed money as collected that Stripe has not told us about, on the
        # one screen whose job is saying what actually moved.
        figures = payments.ledger_figures(payment, payout)

        owner = db.get(User, prop.owner_id)
        award = db.execute(
            select(Award)
            .where(Award.turnover_id == turnover.id)
            .order_by(Award.awarded_at.desc())
        ).scalars().first()

        out.append(
            LedgerRowOut(
                turnover_id=turnover.id,
                property_nickname=prop.nickname,
                checkout_at=turnover.checkout_at,
                cleaner_name=award.cleaner_name if award else "—",
                owner_name=owner.full_name if owner else "—",
                status=payment.status,
                collected_cents=figures["collected_cents"],
                paid_out_cents=figures["paid_out_cents"],
                platform_fee_cents=figures["platform_fee_cents"],
                drift_cents=figures["drift_cents"],
                awaiting_cents=figures["awaiting_cents"],
                unknown_cents=figures["unknown_cents"],
                refunded_amount_cents=payment.refunded_amount_cents,
                failure_message=payment.failure_message,
                created_at=payment.created_at,
            )
        )
    return out


@router.get("/ledger", response_model=LedgerOut)
def ledger(
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> LedgerOut:
    """Collected, paid out, kept — and whether they still add up.

    Totals are summed from the rows on screen rather than queried separately.
    Two queries that could disagree about the same money is how a
    reconciliation page ends up reassuring somebody about a number it never
    actually checked.
    """
    del admin
    rows = _ledger_rows(db)
    return LedgerOut(
        rows=rows,
        total_collected_cents=sum(row.collected_cents for row in rows),
        total_paid_out_cents=sum(row.paid_out_cents for row in rows),
        total_platform_fee_cents=sum(row.platform_fee_cents for row in rows),
        total_drift_cents=sum(row.drift_cents for row in rows),
        # Carried separately and never added into the three above: one is money
        # in flight, the other is money whose fate nobody knows yet.
        total_awaiting_cents=sum(row.awaiting_cents for row in rows),
        total_unknown_cents=sum(row.unknown_cents for row in rows),
    )
