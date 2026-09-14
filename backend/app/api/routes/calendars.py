"""Owner-facing calendar feeds: add one, sync it, take it away.

Every route is owner-scoped through the property, fetched by id **and** owner in
one query — a feed on somebody else's house answers 404, not 403, for the same
reason as everywhere else in this product.

**Nothing here decides sync policy.** `app/services/calendars.py` owns what a
booking becomes and what a person's edit protects; these routes ask it and
render the answer.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.deps import require_role
from app.api.routes.properties import get_owned_property
from app.db import get_db
from app.models.calendar import PropertyCalendar
from app.models.enums import UserRole
from app.models.user import User
from app.schemas.calendar import (
    CalendarCreate,
    CalendarOut,
    CalendarUpdate,
    SyncOut,
)
from app.services import calendars

router = APIRouter(prefix="/properties/{property_id}/calendars", tags=["calendars"])

require_owner = require_role(UserRole.OWNER)


def _owned_calendar(
    db: Session, property_id: uuid.UUID, calendar_id: uuid.UUID, owner: User
) -> PropertyCalendar:
    get_owned_property(property_id, db, owner)
    calendar = db.get(PropertyCalendar, calendar_id)
    if calendar is None or calendar.property_id != property_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Calendar not found"
        )
    return calendar


@router.get("", response_model=list[CalendarOut])
def list_calendars(
    property_id: uuid.UUID,
    db: Session = Depends(get_db),
    owner: User = Depends(require_owner),
) -> list[PropertyCalendar]:
    get_owned_property(property_id, db, owner)
    return calendars.for_property(db, property_id)


@router.post("", response_model=SyncOut, status_code=status.HTTP_201_CREATED)
def add_calendar(
    property_id: uuid.UUID,
    payload: CalendarCreate,
    db: Session = Depends(get_db),
    owner: User = Depends(require_owner),
) -> SyncOut:
    """Add a feed **and read it immediately**.

    Syncing on add is the whole point of doing it here: an owner who pasted the
    wrong link finds out now, on the screen where they pasted it, rather than
    tomorrow when no jobs appeared. A feed that fails on the first read is still
    saved — it may be a temporary outage, and throwing away what they typed
    helps nobody — but the error comes back with it.
    """
    prop = get_owned_property(property_id, db, owner)

    # **Asked, not re-derived** — `calendars.refuse_ineligible` is the one
    # author, so this endpoint, the Sync button and the scheduled pass cannot
    # give three answers. This route used to carry its own copy that checked
    # only the residential case, and the archived case fell through it: the
    # calendar was committed, `sync` then refused *after* the block that records
    # a fetch failure, and the handler below swallowed it as though the reason
    # had been written to the row. The owner got a 201, no error, and no jobs.
    try:
        calendars.refuse_ineligible(prop)
    except calendars.CalendarError as refused:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=refused.detail
        ) from None

    calendar = PropertyCalendar(
        property_id=prop.id, url=payload.url, label=payload.label
    )
    db.add(calendar)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That calendar is already connected to this property.",
        ) from None
    db.refresh(calendar)

    try:
        result = calendars.sync(db, calendar)
    except calendars.CalendarError:
        # **A 201 with the reason on the row, not an error.** Adding the
        # calendar genuinely succeeded; it is the *reading* that failed, which
        # may be a temporary outage. Throwing away what the owner typed helps
        # nobody, and `sync` has already written the reason to `last_error`
        # where their screen will show it.
        result = calendars.SyncResult()

    db.refresh(calendar)
    return _answer(calendar, result)


def _answer(calendar: PropertyCalendar, result: calendars.SyncResult) -> SyncOut:
    return SyncOut(
        calendar=CalendarOut.model_validate(calendar),
        bookings_seen=result.bookings_seen,
        created=result.created,
        updated=result.updated,
        removed=result.removed,
        stale_but_kept=result.stale_but_kept,
    )


@router.post("/{calendar_id}/sync", response_model=SyncOut)
def sync_calendar(
    property_id: uuid.UUID,
    calendar_id: uuid.UUID,
    db: Session = Depends(get_db),
    owner: User = Depends(require_owner),
) -> SyncOut:
    """Read it now, rather than waiting for the scheduled pass.

    Safe to press repeatedly: identity comes from the booking's own id, so a
    second sync of an unchanged feed writes nothing.
    """
    calendar = _owned_calendar(db, property_id, calendar_id, owner)
    try:
        result = calendars.sync(db, calendar)
    except calendars.CalendarError as error:
        # **The row may be gone**, which is one of the things `sync` can refuse
        # for: another request can delete the feed while this one is out on the
        # network. Refreshing it unconditionally then raises inside the handler
        # and turns a deliberate 502 into a 500 that says nothing.
        #
        # Asked for forgiveness rather than permission, because the obvious
        # guard does not work: `db.get` answers from the session's identity map
        # and hands back the deleted instance without touching the database, so
        # a `is not None` check passes and the refresh fails anyway. That is the
        # same stale-cache trap as `_claim`, one layer up.
        try:
            db.refresh(calendar)
        except SQLAlchemyError:
            pass
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=error.detail
        ) from None

    db.refresh(calendar)
    return _answer(calendar, result)


@router.patch("/{calendar_id}", response_model=CalendarOut)
def update_calendar(
    property_id: uuid.UUID,
    calendar_id: uuid.UUID,
    payload: CalendarUpdate,
    db: Session = Depends(get_db),
    owner: User = Depends(require_owner),
) -> PropertyCalendar:
    """Rename it, or switch it off. The URL is deliberately not editable."""
    calendar = _owned_calendar(db, property_id, calendar_id, owner)
    for field, value in payload.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(calendar, field, value)
    db.commit()
    db.refresh(calendar)
    return calendar


@router.delete(
    "/{calendar_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None
)
def remove_calendar(
    property_id: uuid.UUID,
    calendar_id: uuid.UUID,
    db: Session = Depends(get_db),
    owner: User = Depends(require_owner),
) -> None:
    """Disconnect the feed. **The jobs it proposed stay.**

    The foreign key is `SET NULL`, so turnovers keep existing and simply stop
    claiming a source. Anything else would mean removing a calendar could
    cancel a job a cleaner is booked on — a settings change with a consequence
    nobody would expect it to have.
    """
    calendar = _owned_calendar(db, property_id, calendar_id, owner)
    db.delete(calendar)
    db.commit()
