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

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.deps import require_role
from app.api.routes.properties import get_owned_property
from app.db import get_db
from app.models.calendar import PropertyCalendar
from app.models.enums import UserRole
from app.models.user import User
from app.schemas.calendar import (
    CalendarFileJobsOut,
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


@router.post("/read-file", response_model=CalendarFileJobsOut)
def read_calendar_file(
    property_id: uuid.UUID,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    owner: User = Depends(require_owner),
) -> CalendarFileJobsOut:
    """Read an uploaded `.ics` and say what cleans it implies. **Writes nothing.**

    For the owner whose listing site will export a file but will not hand over
    a sync URL. It is deliberately *not* a second kind of feed: no
    `property_calendars` row, no `external_ref`, no adoption and nothing
    reconciled later. It fills in the bulk form and then it is over, and the
    rows the owner submits are ordinary turnovers they own.

    That distinction is the whole reason this is safe to add cheaply. Every
    rule in `calendars.py` about identity and vanishing bookings exists because
    a feed keeps talking; a file does not, and giving a one-shot upload that
    machinery would have meant maintaining rules with nothing behind them.

    No network either, which means none of the request-forgery surface a URL
    carries — this is the one place a calendar can be read without this server
    connecting anywhere.
    """
    prop = get_owned_property(property_id, db, owner)
    try:
        # The one author of "does a booking calendar make sense here", asked
        # rather than copied. A home has no guests checking out, so an `.ics`
        # of stays is a category error on one — the same refusal the feed path
        # gives, in the same words.
        calendars.refuse_ineligible(prop)
    except calendars.CalendarError as refused:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(refused)
        ) from None

    # Bounded before it is read, not after. `MAX_FEED_BYTES` is the same limit
    # the fetch path enforces while streaming, and for the same reason: reading
    # the whole thing to find out how big it is is not a limit.
    raw = file.file.read(calendars.MAX_FEED_BYTES + 1)
    if len(raw) > calendars.MAX_FEED_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"That file is larger than the "
                f"{calendars.MAX_FEED_BYTES // (1024 * 1024)}MB this reads."
            ),
        )
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="That file is empty."
        )

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # Named rather than shrugged at: an owner who uploaded a PDF of their
        # bookings deserves to be told that is what happened.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That does not look like a calendar file (.ics).",
        ) from None

    try:
        bookings = calendars.parse(text)
    except calendars.CalendarError as refused:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(refused)
        ) from None

    # The same two steps the feed takes, so an uploaded file and a synced URL
    # propose the same jobs from the same bookings — including the horizon, the
    # past floor, and which arrival counts as this clean's checkin.
    jobs = calendars.jobs_for(bookings, prop)
    return CalendarFileJobsOut(
        bookings_seen=len(bookings),
        jobs=[
            {"checkout_at": job.checkout_at, "checkin_at": job.checkin_at}
            for job in jobs
        ],
    )


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
    except calendars.CalendarError as error:
        # **A 201 with the reason on the row, not an error.** Adding the
        # calendar genuinely succeeded; it is the *reading* that failed, which
        # may be a temporary outage. Throwing away what the owner typed helps
        # nobody, and `sync` has already written the reason to `last_error`
        # where their screen will show it.
        #
        # Unless the row itself is gone — another request can delete the feed
        # while this one is still fetching, and then there is nothing to answer
        # with and no row carrying the reason.
        if not _refresh_if_present(db, calendar):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=error.detail
            ) from None
        return _answer(calendar, calendars.SyncResult())

    if not _refresh_if_present(db, calendar):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That calendar was removed while it was being read.",
        )
    return _answer(calendar, result)


def _refresh_if_present(db: Session, calendar: PropertyCalendar) -> bool:
    """Re-read the row, unless it is gone. Answers whether it is still there.

    **Both endpoints call this, which is the point.** `sync` can refuse because
    another request deleted the feed while this one was out on the network, and
    an unguarded `db.refresh` then raises *inside the error handler* and turns a
    deliberate answer into a 500 that says nothing. Round five fixed exactly
    that on the sync endpoint and left the identical line on the add endpoint —
    the same rule on one of two paths, which is the mistake this file has now
    made often enough to stop writing the line twice.

    Asked for forgiveness rather than permission, because the obvious guard does
    not work: `db.get` answers from the session's identity map and hands back
    the deleted instance without touching the database, so a `is not None` check
    passes and the refresh fails anyway.
    """
    try:
        db.refresh(calendar)
    except SQLAlchemyError:
        return False
    return True


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
        _refresh_if_present(db, calendar)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=error.detail
        ) from None

    if not _refresh_if_present(db, calendar):
        # **The success path needs this too.** A DELETE already waiting on the
        # calendar's lock can commit the moment `sync` commits, and the failed
        # refresh leaves the instance expired — so serialising it here is a 500
        # at the very end of a request that otherwise worked.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That calendar was removed while it was being read.",
        )
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
