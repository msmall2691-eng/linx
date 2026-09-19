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
    CalendarUrlIn,
    SyncOut,
)
from app.services import calendars

router = APIRouter(prefix="/properties/{property_id}/calendars", tags=["calendars"])

require_owner = require_role(UserRole.OWNER)


def _owned_calendar(
    db: Session, property_id: uuid.UUID, calendar_id: uuid.UUID, owner: User
) -> PropertyCalendar:
    """The calendar behind an id, or 404.

    **An archived calendar answers 404 like any other thing that is not
    there.** The row outlives removal so its jobs keep their identity, but that
    is bookkeeping the owner never sees: to them it is gone, it is off the
    panel, and an endpoint that still let it be renamed or re-read would be
    exposing the bookkeeping as though it were a feature. Reconnecting is
    pasting the address again, not operating on an id no screen shows.
    """
    get_owned_property(property_id, db, owner)
    calendar = db.get(PropertyCalendar, calendar_id)
    if (
        calendar is None
        or calendar.property_id != property_id
        or calendar.removed_at is not None
    ):
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

    # **Reconnecting is adding the same address again**, and it lands here
    # rather than on a button of its own, because that is what an owner does:
    # they do not think "reactivate calendar 7", they paste the link again.
    #
    # The archived row still holds the URL, so the unique constraint is what
    # finds it — the same constraint that refuses a genuine duplicate. Which of
    # the two this is depends only on whether the row it collided with is
    # archived.
    calendar = PropertyCalendar(
        property_id=prop.id, url=payload.url, label=payload.label
    )
    db.add(calendar)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        calendar = calendars.reconnect(
            db, property_id=prop.id, url=payload.url, label=payload.label
        )
        if calendar is None:
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
    """Re-read the row. Answers whether this feed is still connected.

    **Both endpoints call this, which is the point.** `sync` can refuse because
    another request removed the feed while this one was out on the network, and
    an unguarded `db.refresh` then raises *inside the error handler* and turns a
    deliberate answer into a 500 that says nothing. Round five fixed exactly
    that on the sync endpoint and left the identical line on the add endpoint —
    the same rule on one of two paths, which is the mistake this file has now
    made often enough to stop writing the line twice.

    **The question it asks had to change when removal became an archive.** It
    used to mean "is the row still there", because a concurrent DELETE made it
    vanish. Rows do not vanish any more — so that check would now pass for a
    calendar the owner removed a second ago, and the endpoint would answer with
    a cheerful count of jobs for a feed that is gone from their screen. The
    race did not go away; it changed shape, and a check written against the old
    shape silently stops covering it.

    The `try` stays regardless. A row deleted by hand is still possible, the
    foreign key still says `SET NULL`, and the obvious guard does not work:
    `db.get` answers from the session's identity map and hands back the deleted
    instance without touching the database, so a `is not None` check passes and
    the refresh fails anyway.
    """
    try:
        db.refresh(calendar)
    except SQLAlchemyError:
        return False
    return calendar.removed_at is None


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


@router.put("/{calendar_id}/url", response_model=CalendarOut)
def change_calendar_url(
    property_id: uuid.UUID,
    calendar_id: uuid.UUID,
    payload: CalendarUrlIn,
    db: Session = Depends(get_db),
    owner: User = Depends(require_owner),
) -> PropertyCalendar:
    """Point this feed at a different address, keeping its jobs.

    **Its own endpoint rather than a field on the PATCH**, because it is not a
    settings tweak: it invalidates any read already in flight and throws away
    everything the row knows about its last sync. Hidden inside a general
    update, a rename could do that as a side effect.

    It deliberately does **not** sync. The add endpoint does, because there the
    owner has just typed an address and a bad one should fail on the screen
    they typed it on — but that read is also what makes add slow, and here the
    screen presses Sync itself straight afterwards, through the same path with
    the same error handling. One place reads a feed on demand.
    """
    # 404s an archived calendar and anything that is not this owner's, before
    # any of the service's own refusals are reached.
    _owned_calendar(db, property_id, calendar_id, owner)
    try:
        return calendars.change_url(
            db, calendar_id=calendar_id, property_id=property_id, url=payload.url
        )
    except calendars.CalendarError as refused:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=refused.detail
        ) from None
    except IntegrityError:
        # The pre-check inside `change_url` reads without a lock, and an add
        # does not take the property lock at all, so the same address can land
        # between the two. The constraint is what actually holds the invariant;
        # this is it doing its job rather than a case that cannot happen.
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That address is already connected to this property.",
        ) from None


@router.patch("/{calendar_id}", response_model=CalendarOut)
def update_calendar(
    property_id: uuid.UUID,
    calendar_id: uuid.UUID,
    payload: CalendarUpdate,
    db: Session = Depends(get_db),
    owner: User = Depends(require_owner),
) -> PropertyCalendar:
    """Rename it, or switch it off. The URL has its own endpoint above."""
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
    """Disconnect the feed. **The jobs it proposed stay, and so does the row.**

    The jobs staying was never in question: removing a calendar must not cancel
    a job a cleaner is booked on, which is a settings change with a consequence
    nobody would expect it to have.

    The *row* staying is the newer half, and it is what makes the jobs keep
    their identity. A feed has no identity observable from outside — the export
    URL rotates, the event UIDs are arbitrary feed-local strings — so the only
    stable thing those jobs were keyed to was this row. Deleting it left them
    orphaned, and reconnecting proposed every booking a second time.

    So this archives. The row keeps its URL, which is what lets a later add on
    the same address find it, and the turnovers keep pointing at it the whole
    time — there is no window in which their source is unknown.
    """
    calendar = _owned_calendar(db, property_id, calendar_id, owner)
    calendars.archive(db, calendar)
