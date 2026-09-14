"""The admin vetting queue.

ID verification is manual at launch and stays that way on purpose — a human
looks at a photo ID and a reference before a cleaner can bid. This router is
that human's surface.

Two things it will not do:

* **It cannot set `can_take_jobs`.** That column is computed by Postgres from
  the two vetting statuses. An admin moves the statuses; the database decides
  what they add up to. There is no endpoint to override it, because an override
  is exactly how a cleaner ends up able to bid without a completed check.
* **It does not auto-reject on a Checkr "consider".** `refresh` parks such a
  report in `pending` with a note; turning that into a rejection is a human
  decision with a legally defined process behind it.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import require_role
from app.db import get_db
from app.models.cleaner_profile import CleanerProfile
from app.models.document import Document
from app.models.enums import UserRole, VerificationStatus
from app.models.user import User
from app.schemas.admin import DocumentReview, VettingQueueEntry, VettingReview
from app.schemas.cleaner import DocumentOut
from app.services import vetting
from app.services.background_check import BackgroundCheckError, get_provider
from app.services.storage import DocumentStorage, get_storage

router = APIRouter(prefix="/admin", tags=["admin"])

require_admin = require_role(UserRole.ADMIN)


def _entry(profile: CleanerProfile) -> VettingQueueEntry:
    return VettingQueueEntry(
        profile_id=profile.id,
        user_id=profile.user_id,
        full_name=profile.user.full_name,
        email=profile.user.email,
        phone=profile.user.phone,
        id_verification_status=profile.id_verification_status,
        background_check_status=profile.background_check_status,
        background_check_provider_ref=profile.background_check_provider_ref,
        has_insurance_on_file=profile.has_insurance_on_file,
        can_take_jobs=profile.can_take_jobs,
        bio=profile.bio,
        service_radius_miles=profile.service_radius_miles,
        documents=[
            DocumentOut.model_validate(document)
            for document in sorted(profile.documents, key=lambda d: d.created_at, reverse=True)
        ],
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


def _entry_after_write(db: Session, profile_id: uuid.UUID) -> VettingQueueEntry:
    """Build a queue entry from the database once a write has committed.

    `expire_on_commit=False` keeps already-loaded collections alive across a
    commit, so without this the response would describe the state before the
    change. Safe here and only here: after the commit there is nothing pending
    left to lose.
    """
    db.expire_all()
    return _entry(_load_profile(db, profile_id))


def _load_profile(db: Session, profile_id: uuid.UUID) -> CleanerProfile:
    """Read a profile and its documents.

    Deliberately does **not** expire the session. `expire_all()` discards
    un-flushed changes, so calling it between a mutation and its commit
    silently drops the write — which is exactly what happened when this helper
    did the expiring itself. Freshness after a write belongs to
    `_entry_after_write`, which runs once the commit is done.
    """
    profile = db.execute(
        select(CleanerProfile)
        .where(CleanerProfile.id == profile_id)
        .options(selectinload(CleanerProfile.documents), selectinload(CleanerProfile.user))
    ).scalar_one_or_none()
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cleaner not found")
    return profile


@router.get("/vetting-queue", response_model=list[VettingQueueEntry])
def vetting_queue(
    include_cleared: bool = Query(
        default=False, description="Include cleaners who are already cleared to bid."
    ),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[VettingQueueEntry]:
    """Cleaners waiting on a human.

    Ordered oldest first: the person who has been waiting longest is the one to
    look at next, and a 1–2 day review only holds if the queue is worked in the
    order people joined it.
    """
    stmt = (
        select(CleanerProfile)
        .options(selectinload(CleanerProfile.documents), selectinload(CleanerProfile.user))
        .order_by(CleanerProfile.created_at)
    )
    if not include_cleared:
        stmt = stmt.where(CleanerProfile.can_take_jobs.is_(False))

    return [_entry(profile) for profile in db.execute(stmt).scalars().all()]


@router.get("/cleaners/{profile_id}", response_model=VettingQueueEntry)
def read_cleaner(
    profile_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> VettingQueueEntry:
    return _entry(_load_profile(db, profile_id))


@router.get("/documents/{document_id}/file", response_class=StreamingResponse)
def view_document(
    document_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    storage: DocumentStorage = Depends(get_storage),
) -> StreamingResponse:
    """Stream a cleaner's document for review.

    The only way an admin sees an uploaded ID: authenticated, role-gated, and
    served through the app. Documents are never reachable by URL alone.
    """
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    return StreamingResponse(
        storage.open(document.storage_key),
        media_type=document.content_type or "application/octet-stream",
        headers={"Content-Disposition": "inline"},
    )


@router.post("/documents/{document_id}/review", response_model=VettingQueueEntry)
def review_document(
    document_id: uuid.UUID,
    payload: DocumentReview,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> VettingQueueEntry:
    """Approve or reject one uploaded document.

    Approving an ID does not clear the cleaner — that is the separate
    identity decision below, because the spec asks a human to weigh a photo ID
    *and* a reference together rather than tick off one file at a time. An
    approved insurance certificate does flip its flag, since insurance is a
    flag and not a gate at v1.
    """
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    document.status = payload.status
    document.review_notes = payload.notes
    document.reviewed_at = datetime.now(timezone.utc)
    document.reviewed_by_id = admin.id

    profile = _load_profile(db, document.cleaner_profile_id)
    vetting.apply_document_review(profile, document, payload.status)

    profile_id = document.cleaner_profile_id
    db.commit()
    return _entry_after_write(db, profile_id)


@router.post("/cleaners/{profile_id}/id-verification", response_model=VettingQueueEntry)
def review_id_verification(
    profile_id: uuid.UUID,
    payload: VettingReview,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> VettingQueueEntry:
    """Record the human identity decision: photo ID plus one reference.

    This is the manual step the spec insists on keeping. Approving it is half
    of what makes `can_take_jobs` true; the database decides the rest.
    """
    profile = _load_profile(db, profile_id)
    profile.id_verification_status = payload.status
    db.commit()
    return _entry_after_write(db, profile_id)


@router.post("/cleaners/{profile_id}/background-check", response_model=VettingQueueEntry)
def review_background_check(
    profile_id: uuid.UUID,
    payload: VettingReview,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> VettingQueueEntry:
    """Record a background-check outcome by hand.

    Used when there is no vendor configured, and when a vendor returns
    something a human has to weigh.
    """
    profile = _load_profile(db, profile_id)
    profile.background_check_status = payload.status
    db.commit()
    return _entry_after_write(db, profile_id)


@router.post(
    "/cleaners/{profile_id}/background-check/refresh", response_model=VettingQueueEntry
)
def refresh_background_check(
    profile_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    provider=Depends(get_provider),
) -> VettingQueueEntry:
    """Re-read the vendor's current view of a check.

    Never downgrades an approval, and never turns a "consider" into a
    rejection — that decision stays with the reviewer.
    """
    profile = _load_profile(db, profile_id)
    if not profile.background_check_provider_ref:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No vendor reference on this cleaner — there's nothing to refresh.",
        )

    try:
        outcome = provider.refresh(provider_ref=profile.background_check_provider_ref)
    except BackgroundCheckError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from None

    if outcome.status is VerificationStatus.APPROVED:
        profile.background_check_status = VerificationStatus.APPROVED
    elif profile.background_check_status is not VerificationStatus.APPROVED:
        # Anything short of a clear result leaves the cleaner un-cleared, but
        # a already-approved cleaner is not demoted by a later poll.
        profile.background_check_status = outcome.status

    db.commit()
    return _entry_after_write(db, profile_id)
