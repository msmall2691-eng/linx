"""A cleaner's own profile, vetting documents, and background check.

Everything here is scoped to the caller. A cleaner reaches their own profile
and their own documents; there is no path from this router to anyone else's.
The admin side of vetting lives in `admin.py`.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import require_role
from app.db import get_db
from app.models.cleaner_profile import CleanerProfile
from app.models.document import Document
from app.models.enums import DocumentStatus, DocumentType, UserRole, VerificationStatus
from app.models.user import User
from app.schemas.cleaner import (
    CleanerProfileDetailOut,
    CleanerProfileOut,
    CleanerProfileUpsert,
    DocumentOut,
    VettingStateOut,
)
from app.schemas.review import ReputationOut
from app.services import reviews, vetting
from app.services.background_check import BackgroundCheckError, get_provider
from app.services.storage import (
    DocumentStorage,
    DocumentTooLarge,
    UnsupportedDocumentType,
    get_storage,
)

router = APIRouter(prefix="/cleaner", tags=["cleaner"])


def _load_profile(db: Session, user: User) -> CleanerProfile | None:
    return db.execute(
        select(CleanerProfile)
        .where(CleanerProfile.user_id == user.id)
        .options(selectinload(CleanerProfile.documents))
    ).scalar_one_or_none()


def _require_profile(db: Session, user: User) -> CleanerProfile:
    profile = _load_profile(db, user)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Set up your cleaner profile first.",
        )
    return profile


def _fresh_detail(db: Session, profile: CleanerProfile) -> CleanerProfileDetailOut:
    """Build the response from the database, not from memory.

    The session is configured with `expire_on_commit=False` so that serializing
    a response after a commit does not re-query every column. The cost is that
    an already-loaded collection survives the commit unchanged — which, right
    after inserting a document, means answering with the list as it was
    *before* the insert. Expiring first is the same discipline guardrail 1
    states for row locks: never trust an in-memory copy across a commit.
    """
    db.expire(profile)
    return _detail(db, profile)


def _detail(db: Session, profile: CleanerProfile) -> CleanerProfileDetailOut:
    state = vetting.evaluate(profile)
    reputation = reviews.reputation_of(db, profile.user_id)
    return CleanerProfileDetailOut(
        **CleanerProfileOut.model_validate(profile).model_dump(),
        vetting=VettingStateOut(
            can_take_jobs=state.can_take_jobs,
            blockers=list(state.blockers),
            warnings=list(state.warnings),
            summary=state.summary,
        ),
        documents=[
            DocumentOut.model_validate(document)
            for document in sorted(profile.documents, key=lambda d: d.created_at, reverse=True)
        ],
        # The same number an owner reading their bid sees, from the same
        # function. A cleaner told a different figure than the one their
        # customers see has no way to tell which is the real one.
        reputation=ReputationOut(count=reputation.count, average=reputation.average),
    )


@router.get("/profile", response_model=CleanerProfileDetailOut)
def read_my_profile(
    db: Session = Depends(get_db),
    user: User = Depends(require_role(UserRole.CLEANER)),
) -> CleanerProfileDetailOut:
    return _detail(db, _require_profile(db, user))


@router.put("/profile", response_model=CleanerProfileDetailOut)
def upsert_my_profile(
    payload: CleanerProfileUpsert,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(UserRole.CLEANER)),
) -> CleanerProfileDetailOut:
    """Create or update the caller's profile.

    A PUT rather than POST/PATCH because a cleaner has exactly one profile: the
    request describes the whole of it, and repeating it is not an error.
    """
    profile = _load_profile(db, user)
    if profile is None:
        profile = CleanerProfile(user_id=user.id)
        db.add(profile)

    for field, value in payload.model_dump().items():
        setattr(profile, field, value)

    db.commit()
    return _fresh_detail(db, profile)


@router.post(
    "/documents",
    response_model=CleanerProfileDetailOut,
    status_code=status.HTTP_201_CREATED,
)
def upload_document(
    document_type: DocumentType,
    upload: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_role(UserRole.CLEANER)),
    storage: DocumentStorage = Depends(get_storage),
) -> CleanerProfileDetailOut:
    """Upload an ID, an insurance certificate, or a reference.

    The file is written under a generated key and never served from a public
    path — see `app.services.storage`. Uploading an ID or reference moves the
    cleaner into the admin review queue; it never approves anything.
    """
    profile = _require_profile(db, user)

    try:
        key = storage.save(
            stream=upload.file,
            content_type=upload.content_type or "",
            cleaner_id=profile.id,
        )
    except UnsupportedDocumentType as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Upload a photo or a PDF. ({exc})",
        ) from None
    except DocumentTooLarge as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)
        ) from None

    document = Document(
        cleaner_profile_id=profile.id,
        type=document_type,
        status=DocumentStatus.PENDING,
        storage_key=key,
        original_filename=upload.filename,
        content_type=upload.content_type,
    )
    db.add(document)
    vetting.note_document_uploaded(profile, document_type)

    db.commit()
    return _fresh_detail(db, profile)


@router.get("/documents/{document_id}/file", response_class=StreamingResponse)
def download_my_document(
    document_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(UserRole.CLEANER)),
    storage: DocumentStorage = Depends(get_storage),
) -> StreamingResponse:
    """Stream back one of the caller's own documents.

    Joined to the caller's profile in the query, so there is no way to reach
    someone else's ID by changing the id in the URL.
    """
    document = db.execute(
        select(Document)
        .join(CleanerProfile, Document.cleaner_profile_id == CleanerProfile.id)
        .where(Document.id == document_id, CleanerProfile.user_id == user.id)
    ).scalar_one_or_none()
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    return StreamingResponse(
        storage.open(document.storage_key),
        media_type=document.content_type or "application/octet-stream",
        headers={"Content-Disposition": "inline"},
    )


@router.delete("/documents/{document_id}", response_model=CleanerProfileDetailOut)
def delete_my_document(
    document_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(UserRole.CLEANER)),
    storage: DocumentStorage = Depends(get_storage),
) -> CleanerProfileDetailOut:
    """Withdraw a document that has not been reviewed yet.

    Reviewed documents stay. They are the evidence behind an approval, and the
    record of what a human actually looked at should not be removable by the
    person it clears.
    """
    profile = _require_profile(db, user)

    document = db.execute(
        select(Document).where(
            Document.id == document_id,
            Document.cleaner_profile_id == profile.id,
        )
    ).scalar_one_or_none()
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    if document.status is not DocumentStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This document has already been reviewed and can't be removed.",
        )

    key = document.storage_key
    db.delete(document)
    db.commit()
    # The row is gone before the file is, so a failure here leaves an orphaned
    # file rather than a row pointing at nothing — the harmless direction.
    storage.delete(key)
    return _fresh_detail(db, profile)


@router.post("/background-check", response_model=CleanerProfileDetailOut)
def request_background_check(
    db: Session = Depends(get_db),
    user: User = Depends(require_role(UserRole.CLEANER)),
    provider=Depends(get_provider),
) -> CleanerProfileDetailOut:
    """Order the cleaner's background check.

    With Checkr configured this sends an invitation the cleaner completes
    directly with Checkr, so their SSN and date of birth never reach this
    database. With no key configured an admin runs it by hand; either way the
    check lands in `pending` and the admin queue.
    """
    profile = _require_profile(db, user)

    if profile.background_check_status is VerificationStatus.APPROVED:
        return _detail(db, profile)
    if profile.background_check_status is VerificationStatus.PENDING:
        # Ordering a second check while one is outstanding bills twice and
        # confuses the queue.
        return _detail(db, profile)

    try:
        outcome = provider.request(user=user, profile=profile)
    except BackgroundCheckError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not start the background check: {exc}",
        ) from None

    profile.background_check_status = outcome.status
    if outcome.provider_ref:
        profile.background_check_provider_ref = outcome.provider_ref

    db.commit()
    return _fresh_detail(db, profile)
