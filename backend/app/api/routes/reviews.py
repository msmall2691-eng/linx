"""Writing and reading reviews.

Both sides of a turnover use the same two endpoints — an owner and a cleaner
have the same rights here, which is the point of a mutual system. The role gate
elsewhere in the product separates who may post a job from who may bid on one;
reviewing is the one place the two are symmetrical.

**Nothing in this module decides visibility.** `app/services/reviews.py` is the
only author of `visible_at`, and these routes read it. A route that revealed a
review itself would be a second author of the rule the whole phase is about.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db import get_db
from app.models.review import Review
from app.models.turnover import Turnover
from app.models.user import User
from app.schemas.review import ReputationOut, ReviewIn, ReviewOut, ReviewsOut
from app.services import awards, reviews

router = APIRouter(tags=["reviews"])


def _out(review: Review) -> ReviewOut:
    return ReviewOut.model_validate(review)


def _answer(db: Session, turnover: Turnover, user: User) -> ReviewsOut:
    """The one shape every review endpoint answers with.

    Deliberately says nothing about whether the other side has written. That is
    the one fact the delayed reveal exists to withhold, and a "waiting on them"
    flag would hand it over while the reviews themselves stayed hidden.
    """
    mine = reviews.own_review(db, turnover.id, user)
    visible = [r for r in reviews.visible_for(db, turnover.id) if r.id != (mine.id if mine else None)]

    people = reviews.participants(db, turnover)
    if people is None:
        blocker = "This turnover is not finished, so there is nothing to review yet."
    elif reviews.role_for(people, user) is None:
        blocker = "You were not part of this turnover."
    elif mine is not None:
        blocker = "You have already reviewed this turnover."
    elif reviews.too_late(reviews.all_on(db, turnover.id), reviews.role_for(people, user)):
        # Their review is already out, so yours would be written having read it.
        blocker = (
            "Their review has already been published, so the window for yours "
            "has closed."
        )
    else:
        blocker = None

    return ReviewsOut(
        turnover_id=turnover.id,
        can_review=blocker is None,
        blocker=blocker,
        mine=_out(mine) if mine else None,
        visible=[_out(r) for r in visible],
    )


def _readable_turnover(db: Session, turnover_id: uuid.UUID, user: User) -> Turnover:
    """The turnover, if this person has any business reading its reviews.

    Somebody else's turnover answers 404 rather than 403 — the same rule as
    everywhere else, because a 403 confirms the id exists.
    """
    turnover = db.get(Turnover, turnover_id)
    if turnover is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Turnover not found"
        )

    people = reviews.participants(db, turnover)
    if people is None or reviews.role_for(people, user) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Turnover not found"
        )
    return turnover


@router.get("/turnovers/{turnover_id}/reviews", response_model=ReviewsOut)
def read_reviews(
    turnover_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ReviewsOut:
    """What this person may see about a turnover's reviews."""
    turnover = _readable_turnover(db, turnover_id, user)
    return _answer(db, turnover, user)


@router.post(
    "/turnovers/{turnover_id}/reviews",
    response_model=ReviewsOut,
    status_code=status.HTTP_201_CREATED,
)
def write_review(
    turnover_id: uuid.UUID,
    payload: ReviewIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ReviewsOut:
    """Write this side's review.

    The turnover row is locked before anything about it is read — the same order
    as every other action on this row. Two sides submitting at the same instant
    both have to see whether the other already has, and a check made against a
    stale read is how two reviews end up written with neither revealed.

    The reply is the same shape the GET answers with, so a screen that replaces
    its state with it keeps everything it was showing. If this was the second
    review, both are now visible in it.
    """
    turnover = awards.lock_turnover(db, turnover_id)
    if turnover is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Turnover not found"
        )

    people = reviews.participants(db, turnover)
    if people is None or reviews.role_for(people, user) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Turnover not found"
        )

    try:
        reviews.submit(
            db,
            turnover=turnover,
            author=user,
            rating=payload.rating,
            text=payload.text,
        )
    except reviews.ReviewRefused as refused:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=refused.detail
        ) from None

    # `submit` committed, and the session is `expire_on_commit=False`, so this
    # object is a pre-commit snapshot until it is expired. Nothing in `submit`
    # writes the turnover today, so nothing is wrong yet — which is exactly the
    # kind of "correct by accident" this codebase refuses to leave lying about.
    # Same discipline as `_fresh_detail` elsewhere: build the answer from the
    # database, not from memory.
    db.expire(turnover)
    return _answer(db, turnover, user)


@router.get("/cleaners/{cleaner_id}/reputation", response_model=ReputationOut)
def read_reputation(
    cleaner_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ReputationOut:
    """Somebody's rating, built only from reviews that are visible.

    Any signed-in user may read it: a rating is the thing a marketplace exists
    to make public. What stays private is the *hidden half* — a rating computed
    from unrevealed reviews would leak it by arithmetic, which is why
    `reputation_of` filters on `visible_at` rather than on nothing.
    """
    del user  # the signed-in gate is the authorization; the identity is not used
    reputation = reviews.reputation_of(db, cleaner_id)
    return ReputationOut(count=reputation.count, average=reputation.average)
