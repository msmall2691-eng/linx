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
assuming — `AWARDED` and `IN_PROGRESS` are explicitly refused by the reschedule
and cancel paths below, because changing the time or pulling the job out from
under a cleaner who has already been hired is a different operation with
different consequences (notifying them, re-posting the job, and the late-
cancellation penalty) and belongs to the phase that builds it.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.api.deps import require_role
from app.db import get_db
from app.models.enums import TurnoverStatus, UserRole
from app.models.property import Property
from app.models.turnover import Turnover
from app.models.user import User
from app.schemas.turnover import (
    TurnoverCancel,
    TurnoverCreate,
    TurnoverDetailOut,
    TurnoverOut,
    TurnoverUpdate,
)
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

    turnover = Turnover(
        property_id=prop.id,
        checkout_at=payload.checkout_at,
        checkin_at=payload.checkin_at,
        owner_budget_cents=payload.owner_budget_cents,
        notes=payload.notes,
        status=TurnoverStatus.OPEN if payload.publish else TurnoverStatus.DRAFT,
    )
    apply_derived_fields(turnover)

    db.add(turnover)
    db.commit()
    db.refresh(turnover)
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
        stmt = stmt.where(
            Turnover.status.notin_((TurnoverStatus.COMPLETED, TurnoverStatus.CANCELLED))
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
    db.commit()
    db.refresh(turnover)
    return turnover


@router.post("/{turnover_id}/cancel", response_model=TurnoverDetailOut)
def cancel_turnover(
    turnover_id: uuid.UUID,
    payload: TurnoverCancel,
    db: Session = Depends(get_db),
    owner: User = Depends(require_role(UserRole.OWNER)),
) -> Turnover:
    """Cancel a turnover nobody has been hired for yet.

    Deliberately limited to `draft` and `open`. Cancelling an awarded turnover
    means a cleaner who arranged their day around it has to be told, and that
    path — notification, re-posting, the late-cancellation policy — belongs to
    the phase that builds it. Quietly allowing it here would leave the cleaner
    finding out by showing up.
    """
    turnover = _get_owned_turnover(turnover_id, db, owner, with_property=True)

    if turnover.status is TurnoverStatus.CANCELLED:
        return turnover
    if turnover.status not in EDITABLE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"A turnover that is {turnover.status.value} cannot be cancelled here — "
                "a cleaner is already scheduled for it."
            ),
        )

    turnover.status = TurnoverStatus.CANCELLED
    turnover.cancelled_at = datetime.now(timezone.utc)
    turnover.cancellation_reason = payload.reason
    db.commit()
    db.refresh(turnover)
    return turnover
