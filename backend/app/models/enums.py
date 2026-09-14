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


class DisputeStatus(str, Enum):
    """Where a dispute stands. **Three values, and no `rejected`.**

    A dispute is a disagreement between two people that a human decides, not a
    request the system approves. `RESOLVED` carries the admin's note, which is
    where "we found for the owner" or "we found for the cleaner" lives — as
    prose a person wrote, because the outcomes at this size are not an
    enumerable set and pretending otherwise puts a policy in a column.

    `ACKNOWLEDGED` is deliberately separate from `OPEN`: "a person has picked
    this up" and "this is settled" are different facts, and collapsing them
    means an admin working a backlog cannot tell what they have already read.
    It does not send anything — the person who raised it got a receipt when
    they did, and hears again at the outcome. It shows on their own view of
    the dispute, so the queue is not a void without being a mailing list.
    """

    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


class DisputeReason(str, Enum):
    """What kind of problem this is, chosen by the person raising it.

    A short closed list, because the free-text description is where the detail
    belongs — the category exists so an admin can see at a glance whether they
    are about to read about money or about safety, and the safety ones are not
    allowed to sit behind twenty billing questions.
    """

    QUALITY = "quality"
    ACCESS = "access"
    DAMAGE = "damage"
    PAYMENT = "payment"
    CONDUCT = "conduct"
    OTHER = "other"


class NotificationEvent(str, Enum):
    """The fixed event list from CLAUDE.md, as a native Postgres enum.

    A native type rather than free text, because the list is meant to be closed:
    adding an event is a migration and therefore a decision somebody makes on
    purpose, not a string that appears in one call site and nowhere else.

    The list started at twelve and is now thirteen. `JOB_COMPLETED` was added in
    phase 6 with the transition it belongs to: the owner is charged for a
    finished job rather than a booked one, so "the cleaner says it is done" went
    from being nobody's business to being the moment the owner has to act on.
    Nobody learning about it means nobody pays and the cleaner is never paid —
    which is exactly the question this list exists to force somebody to answer
    out loud. Adding it was a migration, on purpose.

    As of phase 7 every one of the thirteen had a sender. The list spent phases
    5 and 6 with declared-and-unwired entries, tested as such so that a phase
    could not quietly skip one; `REVIEW_RECEIVED` was the last of them, and it
    fires on reveal rather than on write. `test_notifications.py` now asserts
    the stronger thing — that nothing is declared with nothing to fire it.

    **Phase 8 opened it again, to fifteen.** Disputes got a real table and a way
    to raise one, and a complaint nobody is told about is the same as no
    complaint at all:

    * `DISPUTE_RAISED` goes to an admin **and back to the person who raised
      it**. The receipt is not a courtesy: somebody who reports that a stranger
      was in their house and hears nothing assumes it went nowhere, and either
      raises it again or phones. The *other side is deliberately not told yet* —
      at this size a human decides when to involve somebody in a complaint
      about them, which is the whole reason the inbox is a person rather than
      a workflow.
    * `DISPUTE_RESOLVED` goes to both parties, carrying the admin's note. It is
      the only place either side finds out what was decided.

    `ACKNOWLEDGED` sends nothing on purpose — see `DisputeStatus`.
    """

    TURNOVER_POSTED = "turnover_posted"
    BID_RECEIVED = "bid_received"
    BID_ACCEPTED = "bid_accepted"
    BID_DECLINED = "bid_declined"
    TURNOVER_REMINDER = "turnover_reminder"
    CLEANER_CANCELLED = "cleaner_cancelled"
    CLEANER_NO_SHOW = "cleaner_no_show"
    OWNER_CANCELLED_AWARDED = "owner_cancelled_awarded"
    TURNOVER_UNCLAIMED = "turnover_unclaimed"
    JOB_COMPLETED = "job_completed"
    PAYMENT_RECEIPT = "payment_receipt"
    PAYOUT_NOTICE = "payout_notice"
    REVIEW_RECEIVED = "review_received"
    DISPUTE_RAISED = "dispute_raised"
    DISPUTE_RESOLVED = "dispute_resolved"


class NotificationChannel(str, Enum):
    """How it reaches a person.

    Email is the only channel with a sender at v1. SMS is declared because the
    product needs it for day-of reminders and it changes the row's shape, not
    just its delivery — but a vendor client written against no account is the
    thing `background_check.py` warns about, so it arrives with its provider.
    """

    EMAIL = "email"
    SMS = "sms"


class NotificationStatus(str, Enum):
    """`FAILED` is a real outcome, not an absence.

    A delivery whose outcome is unknown stays `PENDING` with `attempted_at`
    set — the same posture guardrail 2 takes on a Stripe call that may or may
    not have gone through. Nothing is assumed sent.
    """

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
