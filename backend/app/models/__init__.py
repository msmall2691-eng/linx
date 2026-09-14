"""SQLAlchemy models.

Every model is imported here so that `Base.metadata` is complete for Alembic
autogenerate and for the test fixtures. A model that is not imported here is
invisible to migrations.
"""

from app.models.award import Award
from app.models.base import Base
from app.models.bid import Bid
from app.models.cleaner_profile import CleanerProfile
from app.models.dispute import Dispute
from app.models.document import Document
from app.models.enums import (
    BidStatus,
    DisputeReason,
    DisputeStatus,
    DocumentStatus,
    DocumentType,
    NotificationChannel,
    NotificationEvent,
    NotificationStatus,
    PaymentStatus,
    TurnoverStatus,
    TurnoverUrgency,
    UserRole,
    VerificationStatus,
)
from app.models.notification import Notification
from app.models.payment import PaymentIn, Payout
from app.models.property import Property
from app.models.review import Review
from app.models.turnover import Turnover
from app.models.user import User

__all__ = [
    "Award",
    "Base",
    "Bid",
    "BidStatus",
    "CleanerProfile",
    "Dispute",
    "DisputeReason",
    "DisputeStatus",
    "Document",
    "DocumentStatus",
    "DocumentType",
    "Notification",
    "NotificationChannel",
    "NotificationEvent",
    "NotificationStatus",
    "PaymentIn",
    "PaymentStatus",
    "Payout",
    "Property",
    "Review",
    "Turnover",
    "TurnoverStatus",
    "TurnoverUrgency",
    "User",
    "UserRole",
    "VerificationStatus",
]
