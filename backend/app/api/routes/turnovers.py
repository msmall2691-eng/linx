"""Owner-facing turnover endpoints.

Same scoping discipline as properties: a turnover is reached through its
property's owner in a single query, and a turnover belonging to someone else
answers 404 rather than confirming the id exists.

One shape rule: **every endpoint that returns a single turnover returns
`TurnoverDetailOut`**, and only the list returns the lighter `TurnoverOut`. A
detail screen replaces its state with whatever an action answers, so an action
returning a smaller shape than the GET silently drops the nested property and
blanks the page on the next render — a dead screen right after a click, with no
error anywhere. One shape, no trap.

Status transitions live here and nowhere else. Each endpoint owns exactly one
move, and every one of them states which statuses it accepts rather than
assuming. Rescheduling still refuses an awarded turnover — moving the time under
a cleaner who has arranged their day around it is not a PATCH — but cancelling
one is now a real path rather than a refusal: it ends the booking, tells the
cleaner and an admin, and demands a reason (see `app.services.awards`).

**Accepting a bid is guardrail 1.** The lock comes first, before anything is
read about the turnover — including who owns it — and everything through the
commit happens inside it. `app.services.awards` holds the rule; these routes
hold the HTTP around it.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.api.deps import require_role
from app.db import get_db
from app.models.bid import Bid
from app.models.cleaner_profile import CleanerProfile
from app.models.enums import BidStatus, TurnoverStatus, UserRole
from app.models.property import Property
from app.models.turnover import Turnover
from app.models.user import User
from app.schemas.award import AwardCancel, BidderOut, TurnoverBidOut
from app.schemas.review import ReputationOut
from app.schemas.turnover import (
    TurnoverCancel,
    TurnoverCreate,
    TurnoverDetailOut,
    TurnoverOut,
    TurnoverUpdate,
)
from app.services import awards, notifications, reviews
from app.services import turnovers as turnover_rules
from app.services.turnovers import apply_derived_fields, refresh_urgency

router = APIRouter(prefix="/turnovers", tags=["turnovers"])

#: Reschedulable only before anyone has been hired.
EDITABLE_STATUSES = (TurnoverStatus.DRAFT, TurnoverStatus.OPEN)


def _get_owned_turnover(
    turnover_id: uuid.UUID,
    db: Session,
    owner: User,
    *,
    with_property: bool = False,
) -> Turnover:
    stmt = (
        select(Turnover)
        .join(Property, Turnover.property_id == Property.id)
        .where(Turnover.id == turnover_id, Property.owner_id == owner.id)
    )
    if with_property:
        stmt = stmt.options(joinedload(Turnover.property))

    turnover = db.execute(stmt).scalar_one_or_none()
    if turnover is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Turnover not found")
    return turnover


def _lock_owned_turnover(turnover_id: uuid.UUID, db: Session, owner: User) -> Turnover:
    """Guardrail 1's opening move: lock the row, *then* look at it.

    Ownership is checked after the lock rather than before, deliberately. The
    check that has to be inside the lock is the one about state, and folding the
    ownership test into the locking query would mean an outer join, which
    `FOR UPDATE` cannot be applied across. A missing id and someone else's id
    answer the same 404 either way.
    """
    turnover = awards.lock_turnover(db, turnover_id)
    if turnover is None or not awards.owns_turnover(db, turnover, owner):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Turnover not found")
    return turnover


def _fresh_detail(db: Session, turnover: Turnover) -> Turnover:
    """Re-read a turnover after writing to it.

    The session is `expire_on_commit=False`, so anything already loaded — the
    awards collection above all — would otherwise answer from before the write,
    and the detail screen would replace its state with a stale booking.
    """
    db.expire(turnover)
    return db.execute(
        select(Turnover)
        .where(Turnover.id == turnover.id)
        .options(joinedload(Turnover.property), selectinload(Turnover.awards))
        .execution_options(populate_existing=True)
    ).scalar_one()


@router.post("", response_model=TurnoverDetailOut, status_code=status.HTTP_201_CREATED)
def create_turnover(
    payload: TurnoverCreate,
    db: Session = Depends(get_db),
    owner: User = Depends(require_role(UserRole.OWNER)),
) -> Turnover:
    prop = db.execute(
        select(Property).where(
            Property.id == payload.property_id,
            Property.owner_id == owner.id,
        )
    ).scalar_one_or_none()
    if prop is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Property not found")
    if not prop.is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This property is archived. Restore it before posting a turnover.",
        )

    # What kind of job this is follows from what kind of property it is, and
    # `app/services/turnovers.py` is the only thing that decides. A mismatch is
    # refused rather than quietly corrected: an owner who asked for a move-out
    # clean and silently got a turnover finds out from the cleaner who turned
    # up expecting two hours' work.
    try:
        service_type = turnover_rules.service_type_for(prop, payload.service_type)
        checkin_at = turnover_rules.checkin_for(prop, payload.checkin_at)
    except turnover_rules.JobRefused as refused:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=refused.detail
        ) from None

    turnover = Turnover(
        property_id=prop.id,
        checkout_at=payload.checkout_at,
        checkin_at=checkin_at,
        service_type=service_type,
        owner_budget_cents=payload.owner_budget_cents,
        notes=payload.notes,
        status=TurnoverStatus.OPEN if payload.publish else TurnoverStatus.DRAFT,
    )
    apply_derived_fields(turnover)

    db.add(turnover)
    if turnover.status is TurnoverStatus.OPEN:
        db.flush()
        notifications.turnover_posted(db, turnover, prop)
    db.commit()
    db.refresh(turnover)
    notifications.deliver_pending(db)
    return turnover


@router.get("", response_model=list[TurnoverOut])
def list_turnovers(
    status_filter: TurnoverStatus | None = Query(default=None, alias="status"),
    property_id: uuid.UUID | None = Query(default=None),
    include_finished: bool = Query(
        default=False,
        description="Include completed and cancelled turnovers.",
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    owner: User = Depends(require_role(UserRole.OWNER)),
) -> list[Turnover]:
    stmt = (
        select(Turnover)
        .join(Property, Turnover.property_id == Property.id)
        .where(Property.owner_id == owner.id)
    )

    if status_filter is not None:
        stmt = stmt.where(Turnover.status == status_filter)
    elif not include_finished:
        # A finished job the owner can still review is **not** finished with
        # them. Phase 7 made the review window close for good once the other
        # side's is published, so a job that only appears behind a toggle is a
        # window somebody misses by never finding the toggle. `reviews.py` owns
        # the rule; this asks it rather than re-deriving it.
        reviewable = reviews.open_review_windows(db, owner)
        unfinished = Turnover.status.notin_(
            (TurnoverStatus.COMPLETED, TurnoverStatus.CANCELLED)
        )
        stmt = stmt.where(
            or_(unfinished, Turnover.id.in_(reviewable)) if reviewable else unfinished
        )

    if property_id is not None:
        stmt = stmt.where(Turnover.property_id == property_id)

    # Soonest checkout first: the thing an owner is about to have a problem with
    # is the one nearest in time, not the one highest on the ladder in a month.
    stmt = stmt.order_by(Turnover.checkout_at).limit(limit).offset(offset)

    turnovers = list(db.execute(stmt).scalars().all())
    refresh_urgency(db, turnovers)
    return turnovers


@router.get("/{turnover_id}", response_model=TurnoverDetailOut)
def read_turnover(
    turnover_id: uuid.UUID,
    db: Session = Depends(get_db),
    owner: User = Depends(require_role(UserRole.OWNER)),
) -> Turnover:
    turnover = _get_owned_turnover(turnover_id, db, owner, with_property=True)
    refresh_urgency(db, [turnover])
    return turnover


@router.patch("/{turnover_id}", response_model=TurnoverDetailOut)
def update_turnover(
    turnover_id: uuid.UUID,
    payload: TurnoverUpdate,
    db: Session = Depends(get_db),
    owner: User = Depends(require_role(UserRole.OWNER)),
) -> Turnover:
    turnover = _get_owned_turnover(turnover_id, db, owner, with_property=True)

    if turnover.status not in EDITABLE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A turnover that is {turnover.status.value} can no longer be edited.",
        )

    fields = payload.model_dump(exclude_unset=True)
    fields.pop("clear_checkin", None)
    for field, value in fields.items():
        setattr(turnover, field, value)
    if payload.clear_checkin:
        turnover.checkin_at = None

    if turnover.checkin_at is not None and turnover.checkin_at < turnover.checkout_at:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="checkin_at cannot be before checkout_at",
        )

    # The schedule may have moved, so the ladder is recomputed — never carried
    # over from the old times.
    apply_derived_fields(turnover)

    db.commit()
    db.refresh(turnover)
    return turnover


@router.post("/{turnover_id}/publish", response_model=TurnoverDetailOut)
def publish_turnover(
    turnover_id: uuid.UUID,
    db: Session = Depends(get_db),
    owner: User = Depends(require_role(UserRole.OWNER)),
) -> Turnover:
    """Move a draft onto the bench so cleaners can see and bid on it."""
    turnover = _get_owned_turnover(turnover_id, db, owner, with_property=True)

    if turnover.status is TurnoverStatus.OPEN:
        return turnover
    if turnover.status is not TurnoverStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A turnover that is {turnover.status.value} cannot be posted.",
        )

    turnover.status = TurnoverStatus.OPEN
    apply_derived_fields(turnover)

    # Every cleared cleaner whose service area covers this property. Queued in
    # the same transaction as the status change, so a posted turnover and the
    # message saying so cannot disagree.
    prop = db.execute(
        select(Property).where(Property.id == turnover.property_id)
    ).scalar_one()
    notifications.turnover_posted(db, turnover, prop)

    db.commit()
    db.refresh(turnover)
    notifications.deliver_pending(db)
    return turnover


@router.post("/{turnover_id}/cancel", response_model=TurnoverDetailOut)
def cancel_turnover(
    turnover_id: uuid.UUID,
    payload: TurnoverCancel,
    db: Session = Depends(get_db),
    owner: User = Depends(require_role(UserRole.OWNER)),
) -> Turnover:
    """Call the job off.

    Two different things wear this name, and the difference is who has already
    arranged their day around it:

    * **Nobody hired yet** (`draft`, `open`) — bookkeeping. The job stops being
      visible and that is the end of it.
    * **A cleaner is booked** (`awarded`) — a person loses work they were
      counting on, so a reason is required, the award is cancelled rather than
      erased, and the cleaner and an admin are told. Not allowed silently, and
      not allowed at all without saying why.

    An `in_progress` turnover is refused: somebody is in the house. That is a
    dispute, not a cancellation, and it goes to the human inbox.
    """
    turnover = _lock_owned_turnover(turnover_id, db, owner)

    if turnover.status is TurnoverStatus.CANCELLED:
        return _fresh_detail(db, turnover)

    if turnover.status is TurnoverStatus.AWARDED:
        award = awards.live_award(db, turnover.id)
        if award is None:
            # Awarded with no live award is a contradiction the guardrail is
            # supposed to make impossible. Refuse rather than paper over it.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This turnover is awarded but has no live booking. Contact support.",
            )
        if not payload.reason:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "A cleaner is booked for this turnover. "
                    "Give a reason — they are told why it was cancelled."
                ),
            )
        awards.cancel_award(
            db,
            turnover=turnover,
            award=award,
            actor=owner,
            reason=payload.reason,
            reopen=False,
        )
        return _fresh_detail(db, turnover)

    if turnover.status not in EDITABLE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"A turnover that is {turnover.status.value} cannot be cancelled here — "
                "the cleaning is already under way."
            ),
        )

    turnover.status = TurnoverStatus.CANCELLED
    turnover.cancelled_at = datetime.now(timezone.utc)
    turnover.cancellation_reason = payload.reason
    db.commit()
    return _fresh_detail(db, turnover)


@router.get("/{turnover_id}/bids", response_model=list[TurnoverBidOut])
def list_turnover_bids(
    turnover_id: uuid.UUID,
    db: Session = Depends(get_db),
    owner: User = Depends(require_role(UserRole.OWNER)),
) -> list[TurnoverBidOut]:
    """Who has bid, what they charge, and whether they are cleared.

    Cheapest first — the number is the reason an owner opened this screen — with
    the ones who can actually be accepted ahead of the ones who cannot.
    """
    turnover = _get_owned_turnover(turnover_id, db, owner)

    rows = db.execute(
        select(Bid, User, CleanerProfile)
        .join(User, Bid.cleaner_id == User.id)
        .outerjoin(CleanerProfile, CleanerProfile.user_id == User.id)
        .where(Bid.turnover_id == turnover.id)
        .order_by(Bid.price_cents)
    ).all()

    # Every bidder's rating in one query rather than one each: a job with twenty
    # bids on it must not become twenty-one round trips. `reviews.py` owns the
    # definition — this asks it for several at once, it does not re-derive.
    reputations = reviews.reputation_counts(db, [user.id for _, user, _ in rows])

    return [
        TurnoverBidOut(
            id=bid.id,
            price_cents=bid.price_cents,
            message=bid.message,
            status=bid.status,
            created_at=bid.created_at,
            updated_at=bid.updated_at,
            cleaner=BidderOut(
                id=user.id,
                full_name=user.full_name,
                bio=profile.bio if profile else None,
                # Straight from the column Postgres generates. Never re-derived.
                can_take_jobs=bool(profile.can_take_jobs) if profile else False,
                has_insurance_on_file=bool(profile.has_insurance_on_file) if profile else False,
                reputation=ReputationOut(
                    count=reputations[user.id].count,
                    average=reputations[user.id].average,
                ),
            ),
        )
        for bid, user, profile in rows
    ]


@router.post("/{turnover_id}/bids/{bid_id}/accept", response_model=TurnoverDetailOut)
def accept_bid(
    turnover_id: uuid.UUID,
    bid_id: uuid.UUID,
    db: Session = Depends(get_db),
    owner: User = Depends(require_role(UserRole.OWNER)),
) -> Turnover:
    """Hire one cleaner. **This is the guardrail 1 endpoint.**

    The row lock is taken first and held through the commit, so two owners'
    tabs — or two taps on a slow connection — cannot both come back a winner.
    The second one to reach the lock finds the turnover awarded and is refused.
    """
    turnover = _lock_owned_turnover(turnover_id, db, owner)

    try:
        awards.accept_bid(db, turnover=turnover, bid_id=bid_id)
    except awards.BidNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Bid not found"
        ) from None
    except awards.AwardConflict as conflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=conflict.detail
        ) from None

    return _fresh_detail(db, turnover)


@router.post("/{turnover_id}/bids/{bid_id}/decline", response_model=list[TurnoverBidOut])
def decline_bid(
    turnover_id: uuid.UUID,
    bid_id: uuid.UUID,
    db: Session = Depends(get_db),
    owner: User = Depends(require_role(UserRole.OWNER)),
) -> list[TurnoverBidOut]:
    """Say no to one bid without hiring anyone.

    Answers with the whole bid list — the same shape the screen loaded — rather
    than the single bid it changed, so a screen that replaces its state with the
    reply still has everything it was showing.
    """
    turnover = _get_owned_turnover(turnover_id, db, owner)

    bid = db.execute(
        select(Bid).where(Bid.id == bid_id, Bid.turnover_id == turnover.id)
    ).scalar_one_or_none()
    if bid is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bid not found")

    if bid.status is BidStatus.ACCEPTED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This bid was accepted — the cleaner is booked. "
                "Cancel the turnover instead, so they are told."
            ),
        )

    if bid.status is BidStatus.SUBMITTED:
        bid.status = BidStatus.DECLINED
        prop = db.get(Property, turnover.property_id)
        if prop is not None:
            notifications.bids_declined(db, turnover, prop, [bid])
        db.commit()
        notifications.deliver_pending(db)

    return list_turnover_bids(turnover_id, db, owner)


@router.post("/{turnover_id}/no-show", response_model=TurnoverDetailOut)
def report_no_show(
    turnover_id: uuid.UUID,
    payload: AwardCancel,
    db: Session = Depends(get_db),
    owner: User = Depends(require_role(UserRole.OWNER)),
) -> Turnover:
    """The booked cleaner never turned up.

    Same machinery as a cancellation and deliberately so — the booking ends, the
    job goes back on the bench, everyone is told — with one difference that
    matters later: the award is flagged `was_no_show`, because nobody gave
    notice. A cancellation is a person changing their plans; this is an owner
    standing in an uncleaned house, and the policy response is not the same.
    """
    turnover = _lock_owned_turnover(turnover_id, db, owner)

    # Both live-booking statuses. Keying on AWARDED alone would mean a cleaner
    # who taps "I'm on site" from the driveway and drives away has made the
    # no-show unrecordable — and `was_no_show` is the history a dispute is
    # argued from. A completed job is a different conversation: that is a
    # dispute and a refund, not a no-show.
    if turnover.status not in awards.LIVE_BOOKING_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This turnover is {turnover.status.value}, so there is nobody booked "
                "to report as a no-show."
            ),
        )
    award = awards.live_award(db, turnover.id)
    if award is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This turnover is awarded but has no live booking. Contact support.",
        )

    awards.cancel_award(
        db,
        turnover=turnover,
        award=award,
        actor=owner,
        reason=payload.reason,
        no_show=True,
        reopen=True,
    )
    return _fresh_detail(db, turnover)
