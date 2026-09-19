"""The bench board: open turnovers near a cleaner, and bidding on them.

Two rules govern this router.

**The bidding gate is `can_take_jobs`, read in one place.** Every route that
lets a cleaner act on a job goes through `_require_cleared_profile`, which asks
`app.services.vetting` — the same function that produces the badge the cleaner
sees on their own profile. There is no second copy of the rule, so the gate and
the badge cannot disagree.

**The board shows less than the owner's view.** Responses use the `board`
schemas, which withhold the street address and the access notes. A cleaner who
has merely bid has not been hired and has no business holding the gate code.

The `/board/jobs` routes are the other side of that line: they answer for jobs
this cleaner has actually been *awarded*, and only those, so the address and the
gate code appear there. The boundary is a live award — not a bid, not a past
booking that was cancelled. Cancelling a job goes through the same service the
owner's side uses, so a cleaner backing out cannot take a shortcut past the
re-post and the alerts.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Float, cast, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import require_role
from app.db import get_db
from app.models.award import Award
from app.models.bid import Bid
from app.models.cleaner_profile import CleanerProfile
from app.models.enums import BidStatus, TurnoverStatus, UserRole
from app.models.property import Property
from app.models.turnover import Turnover
from app.models.user import User
from app.schemas.board import (
    AwardedJobOut,
    AwardedPropertyOut,
    BidCreate,
    BoardBidOut,
    BoardPropertyOut,
    BoardTurnoverOut,
    JobCancel,
)
from app.schemas.award import ArrivalIn
from app.services import awards, notifications, vetting
from app.services.geo import distance_miles_sql, haversine_miles
from app.services.turnovers import refresh_urgency

router = APIRouter(prefix="/board", tags=["board"])

#: Urgency, most urgent first. Postgres orders a native enum by its declared
#: order, which runs least-urgent first, so the board asks for it descending.
URGENCY_DESC = Turnover.urgency.desc()


def _require_profile(db: Session, user: User) -> CleanerProfile:
    profile = db.execute(
        select(CleanerProfile).where(CleanerProfile.user_id == user.id)
    ).scalar_one_or_none()
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Set up your cleaner profile first.",
        )
    return profile


def _require_cleared_profile(db: Session, user: User) -> CleanerProfile:
    """The bidding gate. One gate, one reason, one source of truth."""
    profile = _require_profile(db, user)
    state = vetting.evaluate(profile)
    if not state.can_take_jobs:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=state.summary)
    return profile


def _serialize(
    turnover: Turnover, distance: float, own_bid: Bid | None
) -> BoardTurnoverOut:
    return BoardTurnoverOut(
        id=turnover.id,
        checkout_at=turnover.checkout_at,
        checkin_at=turnover.checkin_at,
        is_same_day=turnover.is_same_day,
        service_type=turnover.service_type,
        status=turnover.status,
        urgency=turnover.urgency,
        owner_budget_cents=turnover.owner_budget_cents,
        notes=turnover.notes,
        created_at=turnover.created_at,
        property=BoardPropertyOut.model_validate(turnover.property),
        distance_miles=round(distance, 1),
        my_bid=BoardBidOut.model_validate(own_bid) if own_bid else None,
    )


@router.get("", response_model=list[BoardTurnoverOut])
def list_open_turnovers(
    include_past: bool = Query(
        default=False, description="Include turnovers whose checkout has already passed."
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(require_role(UserRole.CLEANER)),
) -> list[BoardTurnoverOut]:
    """Open turnovers inside the cleaner's service radius.

    Readable by any cleaner with a service area, cleared or not — seeing the
    work is how someone decides whether finishing vetting is worth it. Bidding
    is what requires clearance.
    """
    profile = _require_profile(db, user)
    if profile.service_lat is None or profile.service_lng is None:
        # No point in a radius query without a centre. An empty board with a
        # warning on the profile beats an arbitrary default location.
        return []

    distance = distance_miles_sql(
        profile.service_lat, profile.service_lng, Property.lat, Property.lng
    )

    stmt = (
        select(Turnover, distance.label("distance_miles"))
        .join(Property, Turnover.property_id == Property.id)
        .options(selectinload(Turnover.property))
        .where(
            Turnover.status == TurnoverStatus.OPEN,
            Property.is_active.is_(True),
            # A property with no coordinates cannot be placed in a radius.
            Property.lat.is_not(None),
            Property.lng.is_not(None),
            distance <= cast(profile.service_radius_miles, Float),
        )
    )
    if not include_past:
        stmt = stmt.where(Turnover.checkout_at >= datetime.now(timezone.utc))

    # Most urgent first, then soonest — the order a cleaner filling a day wants.
    stmt = stmt.order_by(URGENCY_DESC, Turnover.checkout_at).limit(limit).offset(offset)

    rows = db.execute(stmt).all()
    turnovers = [row[0] for row in rows]
    refresh_urgency(db, turnovers)

    own_bids: dict = {}
    if turnovers:
        own_bids = {
            bid.turnover_id: bid
            for bid in db.execute(
                select(Bid).where(
                    Bid.cleaner_id == user.id,
                    Bid.turnover_id.in_([t.id for t in turnovers]),
                )
            ).scalars()
        }

    return [
        _serialize(turnover, float(row[1]), own_bids.get(turnover.id))
        for turnover, row in zip(turnovers, rows, strict=True)
    ]


def _serialize_job(award: Award, turnover: Turnover, prop: Property) -> AwardedJobOut:
    """One awarded job, in the only shape any job endpoint answers with.

    The access notes follow the award, not the history: they are released while
    the booking is live and gone the moment it is not. One rule, applied in one
    place, so a cancelled job cannot keep handing out a gate code.
    """
    return AwardedJobOut(
        award_id=award.id,
        turnover_id=turnover.id,
        checkout_at=turnover.checkout_at,
        checkin_at=turnover.checkin_at,
        is_same_day=turnover.is_same_day,
        service_type=turnover.service_type,
        status=turnover.status,
        urgency=turnover.urgency,
        notes=turnover.notes,
        agreed_price_cents=award.agreed_price_cents,
        awarded_at=award.awarded_at,
        started_at=award.started_at,
        en_route_at=award.en_route_at,
        # Asked, never re-derived: one author for the threshold.
        arrival_check=awards.arrival_check(award),
        arrival_distance_m=award.arrival_distance_m,
        completed_at=award.completed_at,
        cancelled_at=award.cancelled_at,
        cancellation_reason=award.cancellation_reason,
        was_no_show=award.was_no_show,
        property=AwardedPropertyOut(
            id=prop.id,
            nickname=prop.nickname,
            property_type=prop.property_type,
            address_line1=prop.address_line1,
            address_line2=prop.address_line2,
            city=prop.city,
            state=prop.state,
            postal_code=prop.postal_code,
            bedrooms=prop.bedrooms,
            bathrooms=prop.bathrooms,
            square_feet=prop.square_feet,
            cleaning_notes=prop.cleaning_notes,
            access_notes=prop.access_notes if award.is_live else None,
        ),
    )


def _job_rows(db: Session, cleaner_id: uuid.UUID, *, turnover_id: uuid.UUID | None = None):
    stmt = (
        select(Award, Turnover, Property)
        .join(Turnover, Award.turnover_id == Turnover.id)
        .join(Property, Turnover.property_id == Property.id)
        .where(Award.cleaner_id == cleaner_id)
    )
    if turnover_id is not None:
        stmt = stmt.where(Award.turnover_id == turnover_id)
    return stmt


@router.get("/jobs", response_model=list[AwardedJobOut])
def list_my_jobs(
    include_finished: bool = Query(
        default=False, description="Include bookings that were cancelled."
    ),
    db: Session = Depends(get_db),
    user: User = Depends(require_role(UserRole.CLEANER)),
) -> list[AwardedJobOut]:
    """Every job this cleaner is booked for, soonest checkout first."""
    stmt = _job_rows(db, user.id)
    if not include_finished:
        stmt = stmt.where(Award.cancelled_at.is_(None))

    rows = db.execute(stmt.order_by(Turnover.checkout_at)).all()
    turnovers = [row[1] for row in rows]
    refresh_urgency(db, turnovers)
    return [_serialize_job(award, turnover, prop) for award, turnover, prop in rows]


@router.get("/jobs/{turnover_id}", response_model=AwardedJobOut)
def read_my_job(
    turnover_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(UserRole.CLEANER)),
) -> AwardedJobOut:
    """One awarded job, with the address and the gate code.

    Scoped by live award *and* cleaner in one query: a turnover somebody else
    was awarded answers 404, exactly as a property belonging to another owner
    does — and so does a booking of this cleaner's own that has since been
    cancelled, because access ends with the booking. Their own history is still
    readable from the list, without the access notes.
    """
    row = db.execute(
        _job_rows(db, user.id, turnover_id=turnover_id).where(Award.cancelled_at.is_(None))
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    award, turnover, prop = row
    refresh_urgency(db, [turnover])
    return _serialize_job(award, turnover, prop)


@router.post("/jobs/{turnover_id}/cancel", response_model=AwardedJobOut)
def cancel_my_job(
    turnover_id: uuid.UUID,
    payload: JobCancel,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(UserRole.CLEANER)),
) -> AwardedJobOut:
    """Back out of a job you were awarded.

    Always allowed, and deliberately: a cleaner who cannot say "I can't make it"
    says nothing instead, and the owner finds out when nobody arrives. What the
    product does about it is make the consequences immediate and visible — the
    job goes straight back on the bench, its urgency is re-derived now that
    nobody is staffed for it, and the owner and an admin are told the same
    minute, whether or not it is inside the 48-hour window.
    """
    turnover = awards.lock_turnover(db, turnover_id)
    if turnover is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    award = awards.live_award(db, turnover.id)
    if award is None or award.cleaner_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    # Both live-booking statuses, not just AWARDED. A cleaner who tapped "I'm
    # on site" and then hit a problem must still be able to say so — "always
    # allowed" above is the whole policy, and a cleaner who cannot back out says
    # nothing instead, which is how the owner finds out by arriving.
    if turnover.status not in awards.LIVE_BOOKING_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This turnover is {turnover.status.value} and cannot be cancelled here. "
                "Contact support."
            ),
        )

    awards.cancel_award(
        db,
        turnover=turnover,
        award=award,
        actor=user,
        reason=payload.reason,
        reopen=True,
    )

    # scalar_one, not an assert: the foreign key guarantees the row exists, and
    # an assert is stripped by `python -O`, so it would guarantee nothing.
    prop = db.execute(
        select(Property).where(Property.id == turnover.property_id)
    ).scalar_one()
    return _serialize_job(award, turnover, prop)


@router.post("/jobs/{turnover_id}/on-my-way", response_model=AwardedJobOut)
def i_am_on_my_way(
    turnover_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(UserRole.CLEANER)),
) -> AwardedJobOut:
    """Tell the owner you are on the way.

    The one job signal that answers a question about the future, and so the
    one that sends anything. It is a timestamp from a button press, not a
    position — see `Award.en_route_at`.
    """
    turnover, award = _lock_my_job(db, turnover_id, user)
    try:
        awards.set_out(db, turnover=turnover, award=award)
    except awards.AwardConflict as conflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=conflict.detail
        ) from None

    return _serialize_job(award, turnover, _property_of(db, turnover))


@router.post("/jobs/{turnover_id}/start", response_model=AwardedJobOut)
def start_my_job(
    turnover_id: uuid.UUID,
    payload: ArrivalIn | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(UserRole.CLEANER)),
) -> AwardedJobOut:
    """Say you are on site, optionally confirming you are there.

    Nothing but the owner's screen hangs off this, which is the point: "did
    anybody actually turn up" is a different question from "was it finished",
    and a single flag cannot answer both.

    The body is **optional in the strong sense**. A cleaner who refuses the
    location permission, has no signal, or is on a desktop still marks
    themselves on site exactly as before, and the check answers `unchecked`.
    Making it a requirement would turn a confirmation into a gate on being
    able to say you had turned up.

    What is sent is used to compute one distance from the property and then
    discarded. **It is a claim rather than proof** — the coordinate comes from
    the cleaner's own browser — which is why the answer is three-valued and
    why nothing in the product acts on it automatically.
    """
    turnover, award = _lock_my_job(db, turnover_id, user)
    at = None
    if (
        payload is not None
        and payload.lat is not None
        and payload.lng is not None
        and payload.accuracy_m is not None
    ):
        # All three or none. A position without its accuracy cannot be read,
        # and filling in an optimistic default would be this endpoint
        # manufacturing the confidence the column exists to record honestly.
        at = awards.AtLocation(
            lat=payload.lat, lng=payload.lng, accuracy_m=payload.accuracy_m
        )
    try:
        awards.start_job(db, turnover=turnover, award=award, at=at)
    except awards.AwardConflict as conflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=conflict.detail
        ) from None

    return _serialize_job(award, turnover, _property_of(db, turnover))


@router.post("/jobs/{turnover_id}/complete", response_model=AwardedJobOut)
def complete_my_job(
    turnover_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(UserRole.CLEANER)),
) -> AwardedJobOut:
    """Say the job is done. **This is what makes it payable.**

    Deliberately the cleaner's act rather than a clock rolling past checkout: a
    checkout time that has passed is not evidence that anybody cleaned anything,
    and money should not move on an assumption. It charges nobody — it tells the
    owner, who then pays through Stripe's own page.
    """
    turnover, award = _lock_my_job(db, turnover_id, user)
    try:
        awards.complete_job(db, turnover=turnover, award=award)
    except awards.AwardConflict as conflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=conflict.detail
        ) from None

    return _serialize_job(award, turnover, _property_of(db, turnover))


def _lock_my_job(
    db: Session, turnover_id: uuid.UUID, user: User
) -> tuple[Turnover, Award]:
    """Lock the turnover, then check it is this cleaner's live job.

    Lock first, check second — the same order as every other action on this row,
    so no two of them can interleave on a stale read. Somebody else's job
    answers 404 rather than 403: a 403 would confirm the id exists.
    """
    turnover = awards.lock_turnover(db, turnover_id)
    if turnover is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    award = awards.live_award(db, turnover.id)
    if award is None or award.cleaner_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return turnover, award


def _property_of(db: Session, turnover: Turnover) -> Property:
    return db.execute(
        select(Property).where(Property.id == turnover.property_id)
    ).scalar_one()


@router.get("/{turnover_id}", response_model=BoardTurnoverOut)
def read_open_turnover(
    turnover_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(UserRole.CLEANER)),
) -> BoardTurnoverOut:
    """One open turnover, in the board's reduced shape."""
    profile = _require_profile(db, user)

    turnover = db.execute(
        select(Turnover)
        .join(Property, Turnover.property_id == Property.id)
        .options(selectinload(Turnover.property))
        .where(Turnover.id == turnover_id, Turnover.status == TurnoverStatus.OPEN)
    ).scalar_one_or_none()
    if turnover is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Turnover not found")

    refresh_urgency(db, [turnover])

    own_bid = db.execute(
        select(Bid).where(Bid.turnover_id == turnover.id, Bid.cleaner_id == user.id)
    ).scalar_one_or_none()

    distance = 0.0
    if (
        profile.service_lat is not None
        and profile.service_lng is not None
        and turnover.property.lat is not None
        and turnover.property.lng is not None
    ):
        distance = haversine_miles(
            profile.service_lat,
            profile.service_lng,
            turnover.property.lat,
            turnover.property.lng,
        )

    return _serialize(turnover, distance, own_bid)


@router.put("/{turnover_id}/bid", response_model=BoardBidOut)
def place_bid(
    turnover_id: uuid.UUID,
    payload: BidCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(UserRole.CLEANER)),
) -> Bid:
    """Name a price, or change the price already named.

    A PUT because a cleaner has at most one bid per turnover — the unique
    constraint on (turnover_id, cleaner_id) makes that structural, so an owner
    never sees the same cleaner twice on one job.
    """
    _require_cleared_profile(db, user)

    turnover = db.execute(
        select(Turnover).where(Turnover.id == turnover_id)
    ).scalar_one_or_none()
    if turnover is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Turnover not found")

    if turnover.status is not TurnoverStatus.OPEN:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This turnover is {turnover.status.value} and is no longer taking bids.",
        )

    existing = db.execute(
        select(Bid).where(Bid.turnover_id == turnover.id, Bid.cleaner_id == user.id)
    ).scalar_one_or_none()

    if existing is not None:
        if existing.status is BidStatus.ACCEPTED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This bid has been accepted and can't be changed.",
            )
        existing.price_cents = payload.price_cents
        existing.message = payload.message
        # Re-offering after a decline or a withdrawal puts it back in front of
        # the owner rather than leaving a stale status on a fresh price.
        existing.status = BidStatus.SUBMITTED
        bid = existing
    else:
        bid = Bid(
            turnover_id=turnover.id,
            cleaner_id=user.id,
            price_cents=payload.price_cents,
            message=payload.message,
        )
        db.add(bid)

    # The owner hears about it in the same transaction that records it. The
    # dedupe key includes the price, so re-submitting the same number is silent
    # and changing it is not — a changed price is new information.
    db.flush()
    prop = db.execute(
        select(Property).where(Property.id == turnover.property_id)
    ).scalar_one()
    notifications.bid_received(db, bid, turnover, prop)

    db.commit()
    db.refresh(bid)
    notifications.deliver_pending(db)
    return bid


@router.get("/bids/mine", response_model=list[BoardBidOut])
def list_my_bids(
    db: Session = Depends(get_db),
    user: User = Depends(require_role(UserRole.CLEANER)),
) -> list[Bid]:
    return list(
        db.execute(
            select(Bid).where(Bid.cleaner_id == user.id).order_by(Bid.created_at.desc())
        )
        .scalars()
        .all()
    )


@router.delete("/{turnover_id}/bid", response_model=BoardBidOut)
def withdraw_bid(
    turnover_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(UserRole.CLEANER)),
) -> Bid:
    """Withdraw a bid the owner has not accepted.

    The row is kept and marked withdrawn rather than deleted: the owner may
    already be looking at it, and "this cleaner pulled out" is information,
    where a silently vanishing bid is not.
    """
    bid = db.execute(
        select(Bid).where(Bid.turnover_id == turnover_id, Bid.cleaner_id == user.id)
    ).scalar_one_or_none()
    if bid is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bid not found")

    if bid.status is BidStatus.ACCEPTED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This bid has been accepted — you're booked for this turnover. "
                "Cancelling an awarded job goes through the cancellation flow."
            ),
        )

    bid.status = BidStatus.WITHDRAWN
    db.commit()
    db.refresh(bid)
    return bid
