"""Enumerations shared by the models.

Each of these maps to a native Postgres enum type. Adding a value means writing
a migration — Postgres enums are not silently extensible.
"""

from __future__ import annotations

from enum import Enum


class UserRole(str, Enum):
    OWNER = "owner"
    CLEANER = "cleaner"
    ADMIN = "admin"


class VerificationStatus(str, Enum):
    """Where a vetting step stands. Manual review at v1 (see CLAUDE.md)."""

    NOT_STARTED = "not_started"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class TurnoverStatus(str, Enum):
    DRAFT = "draft"
    OPEN = "open"
    AWARDED = "awarded"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class TurnoverUrgency(str, Enum):
    """Derived from how close checkout is to the next checkin.

    The product's core pricing and priority signal, not decoration. The ladder
    is ordered least to most urgent; `SAME_DAY` means a guest checks in the same
    day the last one checks out.
    """

    STANDARD = "standard"
    SOON = "soon"
    URGENT = "urgent"
    SAME_DAY = "same_day"


class BidStatus(str, Enum):
    SUBMITTED = "submitted"
    WITHDRAWN = "withdrawn"
    ACCEPTED = "accepted"
    DECLINED = "declined"


class DocumentType(str, Enum):
    ID = "id"
    INSURANCE = "insurance"
    REFERENCE = "reference"


class DocumentStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class PaymentStatus(str, Enum):
    """Covers both money legs.

    `REQUIRES_REVIEW` is guardrail 2's landing spot: a row whose Stripe call was
    attempted but whose outcome is unknown. It is never assumed successful and
    never assumed failed — a human looks at it.
    """

    PENDING = "pending"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REFUNDED = "refunded"
    REQUIRES_REVIEW = "requires_review"
