"""Cleaner profiles — bio, service area, vetting state.

`can_take_jobs` is the single honest boolean this file exists to protect. It is
a **Postgres generated column**: the database computes it from the two vetting
statuses and no application code can write it. That is what makes "checked in
exactly one place" true rather than aspirational — a bidding gate and a display
badge physically cannot disagree, because both read the same stored column and
neither can set it.

Insurance is deliberately *not* part of the rule. At v1 a missing COI is a flag
shown prominently, not a gate (see CLAUDE.md) — requiring it up front while
supply is scarce kills the launch.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import VerificationStatus

if TYPE_CHECKING:
    from app.models.document import Document
    from app.models.user import User

#: The one definition of "this cleaner may take jobs".
#:
#: Evaluated by Postgres as a STORED generated column. The initial migration
#: carries a copy of this string because migrations must stand alone as
#: historical snapshots; changing the rule means a new migration, and this
#: constant and that migration change together.
CAN_TAKE_JOBS_SQL = (
    "id_verification_status = 'approved'::verification_status "
    "AND background_check_status = 'approved'::verification_status"
)


class CleanerProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "cleaner_profiles"
    __table_args__ = (
        CheckConstraint(
            "service_radius_miles > 0 AND service_radius_miles <= 200",
            name="service_radius_sane",
        ),
        CheckConstraint(
            "service_lat >= -90 AND service_lat <= 90",
            name="service_lat_range",
        ),
        CheckConstraint(
            "service_lng >= -180 AND service_lng <= 180",
            name="service_lng_range",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    bio: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Service area: a point and a radius. One region at launch — there is no
    # region column and no multi-region logic (see CLAUDE.md).
    service_lat: Mapped[float | None] = mapped_column(Numeric(9, 6), nullable=True)
    service_lng: Mapped[float | None] = mapped_column(Numeric(9, 6), nullable=True)
    service_radius_miles: Mapped[int] = mapped_column(Integer, nullable=False, default=25)

    # Vetting. Both are reviewed by a human at v1; background_check_status is
    # driven by the vendor integration in phase 3.
    id_verification_status: Mapped[VerificationStatus] = mapped_column(
        Enum(
            VerificationStatus,
            name="verification_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=VerificationStatus.NOT_STARTED,
        server_default=VerificationStatus.NOT_STARTED.value,
    )
    background_check_status: Mapped[VerificationStatus] = mapped_column(
        Enum(
            VerificationStatus,
            name="verification_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=VerificationStatus.NOT_STARTED,
        server_default=VerificationStatus.NOT_STARTED.value,
    )
    background_check_provider_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # A flag, not a gate. Surfaced in the UI as "insurance on file: no".
    has_insurance_on_file: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # Stripe Connect (phase 6). Deliberately **not** part of can_take_jobs.
    #
    # Being trusted in a stranger's house and being able to receive money are
    # different questions with different failure modes, and the trust gate has
    # exactly one author. Folding Stripe readiness into the generated column
    # would mean a verification delay on Stripe's side silently stops a vetted
    # cleaner from bidding, with the badge and the gate disagreeing about why.
    # Instead the payment step refuses out loud and says what is missing.
    stripe_account_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, unique=True
    )
    #: Stripe's answer, never ours: true only once Stripe says this account can
    #: receive transfers. Refreshed from the account, not assumed after
    #: onboarding — finishing the form is not the same as passing verification.
    stripe_payouts_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    #: The cleaner has been through the Express onboarding form. Useful for
    #: telling "never started" apart from "started, still under review".
    stripe_details_submitted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    #: Read-only. Computed by Postgres; assigning to it raises on flush.
    can_take_jobs: Mapped[bool] = mapped_column(
        Boolean,
        Computed(CAN_TAKE_JOBS_SQL, persisted=True),
        nullable=False,
    )

    user: Mapped["User"] = relationship(back_populates="cleaner_profile")
    documents: Mapped[list["Document"]] = relationship(
        back_populates="cleaner_profile",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<CleanerProfile user_id={self.user_id} can_take_jobs={self.can_take_jobs}>"
