"""Admin vetting shapes.

The queue exists because ID verification is manual at launch, and deliberately
so: it is the safety valve between "hands off" and anyone walking into a
stranger's house. These shapes are what the reviewer sees.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import DocumentStatus, VerificationStatus
from app.schemas.cleaner import DocumentOut


class VettingReview(BaseModel):
    """An admin's decision on one vetting step.

    `pending` is allowed on purpose: sending something back to pending is how a
    reviewer parks a decision they are not ready to make — notably a Checkr
    "consider" result, which needs the adverse-action process before it can
    become a rejection.
    """

    status: VerificationStatus
    notes: str | None = Field(default=None, max_length=4000)


class DocumentReview(BaseModel):
    status: DocumentStatus
    notes: str | None = Field(default=None, max_length=4000)


class VettingQueueEntry(BaseModel):
    """One cleaner awaiting review."""

    profile_id: uuid.UUID
    user_id: uuid.UUID
    full_name: str
    email: str
    phone: str | None

    id_verification_status: VerificationStatus
    background_check_status: VerificationStatus
    background_check_provider_ref: str | None
    has_insurance_on_file: bool
    can_take_jobs: bool

    bio: str | None
    service_radius_miles: int
    documents: list[DocumentOut]

    created_at: datetime
    updated_at: datetime
