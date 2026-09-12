"""Cleaner-facing shapes: the profile, its vetting state, and vetting documents."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import DocumentStatus, DocumentType, VerificationStatus


class CleanerProfileUpsert(BaseModel):
    """Create or replace the caller's own profile.

    None of the vetting fields appear here. `id_verification_status`,
    `background_check_status` and `can_take_jobs` are not things a cleaner can
    tell us about themselves — the first two move only through the admin queue
    or the vendor, and the third is computed by the database.
    """

    bio: str | None = Field(default=None, max_length=4000)
    service_lat: Decimal | None = Field(default=None, ge=-90, le=90)
    service_lng: Decimal | None = Field(default=None, ge=-180, le=180)
    service_radius_miles: int = Field(default=25, ge=1, le=200)


class VettingStateOut(BaseModel):
    """Why the cleaner can or cannot bid — one answer, from one place.

    Rendered as-is by the UI. A screen that recomputes this from the statuses
    is how a "verified" badge ends up next to a button that refuses.
    """

    can_take_jobs: bool
    blockers: list[str]
    warnings: list[str]
    summary: str


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: DocumentType
    status: DocumentStatus
    original_filename: str | None
    content_type: str | None
    review_notes: str | None
    reviewed_at: datetime | None
    created_at: datetime
    #: Deliberately absent: `storage_key`. It is an internal path, and handing
    #: it to a client invites someone to try fetching it directly.


class CleanerProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    bio: str | None
    service_lat: Decimal | None
    service_lng: Decimal | None
    service_radius_miles: int

    id_verification_status: VerificationStatus
    background_check_status: VerificationStatus
    has_insurance_on_file: bool
    can_take_jobs: bool

    created_at: datetime
    updated_at: datetime


class CleanerProfileDetailOut(CleanerProfileOut):
    """The profile plus everything its own screen needs, in one request."""

    vetting: VettingStateOut
    documents: list[DocumentOut]
