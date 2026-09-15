"""Raising a dispute, and reading your own.

Both sides of a turnover use these endpoints, the same way both sides use the
review endpoints: an owner and a cleaner have identical rights to complain about
a job they were on together. The admin's side of the same table lives in
`app/api/routes/console.py`, because working a queue and filing into it are
different jobs with different shapes.

**Nothing here decides anything about the dispute.** `app/services/disputes.py`
owns who may raise one and what state it can reach; these routes ask it and
render the answer. In particular no route resolves a dispute — that is an admin
action, by hand, every time.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db import get_db
from app.models.award import Award
from app.models.dispute import Dispute
from app.models.property import Property
from app.models.turnover import Turnover
from app.models.user import User
from app.schemas.dispute import (
    DisputableBookingOut,
    DisputeIn,
    DisputeOut,
    DisputesOut,
)
from app.services import disputes

router = APIRouter(tags=["disputes"])


def _out(dispute: Dispute) -> DisputeOut:
    return DisputeOut.model_validate(dispute)


def _answer(db: Session, turnover: Turnover, user: User) -> DisputesOut:
    """The one shape both dispute endpoints answer with.

    Says nothing about disputes the *other* side raised — not their content and
    not their existence. Whether somebody is told they are being complained
    about is a decision a person makes when they work the queue, and a count on
    this screen would make it for them.
    """
    bookings = disputes.disputable_awards(db, turnover, user)
    if not bookings:
        blocker = (
            "Nobody was ever booked for this turnover, so there is no one to "
            "raise a dispute with."
        )
    elif disputes.open_dispute_of(db, turnover.id, user) is not None:
        blocker = (
            "You already have an open dispute on this job. Somebody is looking "
            "at it."
        )
    else:
        blocker = None

    mine = [
        dispute
        for dispute in disputes.raised_by(db, user)
        if dispute.turnover_id == turnover.id
    ]
    return DisputesOut(
        turnover_id=turnover.id,
        can_raise=blocker is None,
        blocker=blocker,
        mine=[_out(dispute) for dispute in mine],
        # Sent whenever they could file, so a screen with a real choice to make
        # can make it. `award_under_dispute` refuses to guess when there is more
        # than one, so a panel that did not ask would be unusable rather than
        # merely quiet.
        bookings=[] if blocker else _bookings(db, bookings),
    )


def _bookings(db: Session, awards: list[Award]) -> list[DisputableBookingOut]:
    """The bookings, named well enough to tell apart."""
    out: list[DisputableBookingOut] = []
    for award in awards:
        cleaner = db.get(User, award.cleaner_id)
        if cleaner is None:
            continue
        out.append(
            DisputableBookingOut(
                award_id=award.id,
                cleaner_name=cleaner.full_name,
                awarded_at=award.awarded_at,
                cancelled_at=award.cancelled_at,
                was_no_show=award.was_no_show,
            )
        )
    return out


def _readable_turnover(db: Session, turnover_id: uuid.UUID, user: User) -> Turnover:
    """The turnover, if this person has any business asking about it.

    Somebody else's answers 404 rather than 403 — the same rule as everywhere
    else, because a 403 confirms the id exists.

    **Owning it is enough to get a real answer**, and that distinction matters
    more than it looks. `disputes.parties` needs an award, so keying this on
    it alone told an owner their own never-awarded turnover did not exist —
    and made the refusal that explains *why* they cannot file
    ("nobody was ever booked… cancel it instead") unreachable from the API.
    The 404 rule is about hiding other people's rows; it was never meant to
    hide somebody's own from them.
    """
    turnover = db.get(Turnover, turnover_id)
    if turnover is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Turnover not found"
        )

    prop = db.get(Property, turnover.property_id)
    if prop is not None and prop.owner_id == user.id:
        return turnover

    # Resolved **per person**: a cleaner whose booking was cancelled and whose
    # turnover has since been re-awarded to somebody else is still party to the
    # job they were booked for, and must not lose sight of their own dispute
    # about it.
    if not disputes.disputable_awards(db, turnover, user):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Turnover not found"
        )
    return turnover


@router.get("/turnovers/{turnover_id}/disputes", response_model=DisputesOut)
def read_disputes(
    turnover_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DisputesOut:
    """This person's own disputes about a turnover, and whether they may file."""
    turnover = _readable_turnover(db, turnover_id, user)
    return _answer(db, turnover, user)


@router.post(
    "/turnovers/{turnover_id}/disputes",
    response_model=DisputesOut,
    status_code=status.HTTP_201_CREATED,
)
def raise_dispute(
    turnover_id: uuid.UUID,
    payload: DisputeIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DisputesOut:
    """File a complaint about this job.

    Answers with the same shape the GET does, so the screen keeps everything it
    was showing and picks up the new row in one round trip.

    **Filing this changes nothing about the money or the booking.** No refund is
    triggered, no award is cancelled, no rating moves. A person reads it and
    decides — which is the policy, not an implementation gap.
    """
    turnover = _readable_turnover(db, turnover_id, user)

    try:
        disputes.raise_dispute(
            db,
            turnover=turnover,
            raiser=user,
            reason=payload.reason,
            description=payload.description,
            award_id=payload.award_id,
        )
    except disputes.DisputeRefused as refused:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=refused.detail
        ) from None

    # `raise_dispute` committed, and the session is `expire_on_commit=False`,
    # so this object is a pre-commit snapshot until it is expired. Same
    # discipline as everywhere else: build the answer from the database.
    db.expire(turnover)
    return _answer(db, turnover, user)


@router.get("/disputes", response_model=list[DisputeOut])
def read_my_disputes(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[DisputeOut]:
    """Everything this person has raised, newest first."""
    return [_out(dispute) for dispute in disputes.raised_by(db, user)]
