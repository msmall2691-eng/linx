"""Owner-facing property endpoints.

Every route here is owner-scoped: a property is looked up by id **and** owner in
one query, never fetched first and checked after. Fetch-then-compare is how an
ownership check gets skipped on the one code path somebody added in a hurry.

A property that belongs to someone else answers 404, not 403. A 403 would
confirm the id exists, which turns this endpoint into a way to enumerate other
people's properties.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import require_role
from app.db import get_db
from app.models.enums import TurnoverStatus, UserRole
from app.models.property import Property
from app.models.turnover import Turnover
from app.models.user import User
from app.schemas.property import PropertyCreate, PropertyOut, PropertyUpdate
from app.services import geocoding

router = APIRouter(prefix="/properties", tags=["properties"])

#: Statuses that mean a turnover is still live work on the schedule.
LIVE_TURNOVER_STATUSES = (
    TurnoverStatus.DRAFT,
    TurnoverStatus.OPEN,
    TurnoverStatus.AWARDED,
    TurnoverStatus.IN_PROGRESS,
)


def get_owned_property(
    property_id: uuid.UUID,
    db: Session,
    owner: User,
) -> Property:
    """Fetch a property by id and owner together, or 404."""
    prop = db.execute(
        select(Property).where(
            Property.id == property_id,
            Property.owner_id == owner.id,
        )
    ).scalar_one_or_none()
    if prop is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Property not found")
    return prop


@router.post("", response_model=PropertyOut, status_code=status.HTTP_201_CREATED)
def create_property(
    payload: PropertyCreate,
    db: Session = Depends(get_db),
    owner: User = Depends(require_role(UserRole.OWNER)),
) -> Property:
    prop = Property(owner_id=owner.id, **payload.model_dump())
    _place(prop)
    db.add(prop)
    db.commit()
    db.refresh(prop)
    return prop


def _place(prop: Property) -> None:
    """Give the property coordinates. **Never leave it without them.**

    The bench board's radius query requires `Property.lat IS NOT NULL`, so a
    property with null coordinates is invisible to every cleaner — on a screen
    that looks completely normal to its owner, who posts a turnover and watches
    nobody bid. Nothing in the app used to set these columns at all; only the
    browser tests did, straight into the database.

    `geocoding.locate` cannot return nothing, which is the point: the failure
    it replaces was a silent null, not a bad guess.
    """
    fix = geocoding.locate(
        lat=prop.lat,
        lng=prop.lng,
        postal_code=prop.postal_code,
        city=prop.city,
    )
    prop.lat, prop.lng = fix.lat, fix.lng


@router.get("", response_model=list[PropertyOut])
def list_properties(
    include_archived: bool = Query(default=False),
    db: Session = Depends(get_db),
    owner: User = Depends(require_role(UserRole.OWNER)),
) -> list[Property]:
    stmt = select(Property).where(Property.owner_id == owner.id)
    if not include_archived:
        stmt = stmt.where(Property.is_active.is_(True))
    stmt = stmt.order_by(Property.nickname)
    return list(db.execute(stmt).scalars().all())


@router.get("/{property_id}", response_model=PropertyOut)
def read_property(
    property_id: uuid.UUID,
    db: Session = Depends(get_db),
    owner: User = Depends(require_role(UserRole.OWNER)),
) -> Property:
    return get_owned_property(property_id, db, owner)


@router.patch("/{property_id}", response_model=PropertyOut)
def update_property(
    property_id: uuid.UUID,
    payload: PropertyUpdate,
    db: Session = Depends(get_db),
    owner: User = Depends(require_role(UserRole.OWNER)),
) -> Property:
    prop = get_owned_property(property_id, db, owner)

    # exclude_unset so an omitted field keeps its value; a PATCH that sent
    # every default would blank out notes the owner never touched.
    fields = payload.model_dump(exclude_unset=True)
    for field, value in fields.items():
        setattr(prop, field, value)

    # Re-place it whenever the address moved, or whenever it somehow has no
    # coordinates — which is every property created before this rule existed.
    address_moved = {"postal_code", "city", "state", "address_line1"} & set(fields)
    sent_coordinates = {"lat", "lng"} & set(fields)
    if address_moved and not sent_coordinates:
        # The address changed and nothing new came with it, so whatever is on
        # the row describes where this property used to be. Keeping it would
        # leave a job advertised at the previous house — worse than a coarse
        # match, because it is confidently wrong.
        prop.lat = prop.lng = None
    if address_moved or sent_coordinates or prop.lat is None or prop.lng is None:
        _place(prop)

    db.commit()
    db.refresh(prop)
    return prop


@router.delete(
    "/{property_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
def archive_property(
    property_id: uuid.UUID,
    db: Session = Depends(get_db),
    owner: User = Depends(require_role(UserRole.OWNER)),
) -> None:
    """Archive a property. It is hidden from the owner's list, not deleted.

    Guardrail 3, applied to a removal: turnovers reference their property for
    the address, the access notes, and the audit trail long after the job is
    done, so a hard delete would take real history with it — and the model's
    `ondelete="CASCADE"` means it would go quietly. Archiving keeps the row.

    Live turnovers block the archive rather than being silently orphaned. A
    property that disappears from the owner's list while a cleaner is still
    scheduled to show up there is exactly the kind of "step removed, nothing
    failed loudly" this rule exists to catch.
    """
    prop = get_owned_property(property_id, db, owner)

    live = db.execute(
        select(func.count())
        .select_from(Turnover)
        .where(
            Turnover.property_id == prop.id,
            Turnover.status.in_(LIVE_TURNOVER_STATUSES),
        )
    ).scalar_one()

    if live:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This property has {live} turnover(s) still scheduled. "
                "Cancel them before archiving it."
            ),
        )

    prop.is_active = False
    db.commit()
