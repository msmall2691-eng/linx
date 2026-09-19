"""The message thread on a booked job, for whichever side is asking.

**One pair of endpoints for both roles**, rather than an owner's and a
cleaner's. Who may read a thread is one question with one answer —
`messages.live_award_for` — and two routes would be two places for that answer
to drift apart, which on this particular question means a channel into somebody's
house opening for the wrong person.

Anything that is not this reader's live booking answers **404**, whether it does
not exist, belongs to somebody else, or has been cancelled. The rule everywhere
in this product: a refusal that tells those apart confirms the id.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db import get_db
from app.models.turnover import Turnover
from app.models.user import User
from app.schemas.message import MessageIn, MessageOut, ThreadOut
from app.services import awards as awards_service
from app.services import messages

router = APIRouter(prefix="/turnovers/{turnover_id}/messages", tags=["messages"])


def _thread(db: Session, turnover_id: uuid.UUID, user: User) -> ThreadOut:
    """Build the whole thread for one reader. **The only place it is built.**

    Both endpoints answer with this, for the reason the rest of the product
    keeps one shape per resource: an action that replied with less than the GET
    would blank the screen that replaced its state with the answer.
    """
    award = messages.live_award_for(db, turnover_id, user)
    if award is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No booking to message about"
        )

    turnover = db.get(Turnover, turnover_id)
    finished = (
        award.completed_at is not None
        and turnover is not None
        and turnover.status not in awards_service.LIVE_BOOKING_STATUSES
    )

    return ThreadOut(
        award_id=award.id,
        can_send=not finished,
        closed_reason=(
            "This job is finished. If something went wrong, raise a dispute."
            if finished
            else None
        ),
        messages=[
            MessageOut(
                id=message.id,
                sender_label=messages.visible_sender(db, message, reader=user),
                mine=message.sender_id == user.id,
                body=message.body,
                sent_at=message.created_at,
            )
            for message in messages.history(db, award)
        ],
    )


@router.get("", response_model=ThreadOut)
def read_thread(
    turnover_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ThreadOut:
    """Everything said on this booking, labelled for whoever is asking."""
    return _thread(db, turnover_id, user)


@router.post("", response_model=ThreadOut, status_code=status.HTTP_201_CREATED)
def send_message(
    turnover_id: uuid.UUID,
    payload: MessageIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ThreadOut:
    """Say something, and the other side is emailed it.

    Answers with the **whole thread** rather than the one message, so the
    screen that replaces its state with the reply keeps everything it had.
    """
    award = messages.live_award_for(db, turnover_id, user)
    turnover = db.get(Turnover, turnover_id)
    if award is None or turnover is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No booking to message about"
        )

    try:
        messages.post(
            db, turnover=turnover, award=award, sender=user, body=payload.body
        )
    except messages.MessageRefused as refused:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=refused.detail
        ) from None

    return _thread(db, turnover_id, user)
