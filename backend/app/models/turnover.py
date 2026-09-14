"""Turnovers — one cleaning job between a checkout and the next checkin.

`urgency` is derived from how close `checkout_at` is to `checkin_at` (or to now,
for a standing vacancy with no next guest booked). The closer, the more urgent.
That ladder is the product's core pricing and priority signal — the derivation
itself lands in phase 2; the column shape is settled here.
"""

from __future__ import annotations

import builtins
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    String,
    UniqueConstraint,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import ServiceType, TurnoverStatus, TurnoverUrgency

if TYPE_CHECKING:
    from app.models.award import Award
    from app.models.bid import Bid
    from app.models.property import Property


class Turnover(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "turnovers"
    __table_args__ = (
        CheckConstraint(
            "checkin_at IS NULL OR checkin_at >= checkout_at",
            name="checkin_after_checkout",
        ),
        CheckConstraint(
            "owner_budget_cents IS NULL OR owner_budget_cents >= 0",
            name="budget_non_negative",
        ),
        # The bench board reads open turnovers ordered by urgency then checkout.
        Index("ix_turnovers_status_checkout_at", "status", "checkout_at"),
        # One job per booking per feed. This is what makes a re-sync idempotent
        # rather than a way to accumulate duplicates every fifteen minutes.
        UniqueConstraint(
            "source_calendar_id", "external_ref", name="uq_turnovers_source_event"
        ),
    )

    property_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("properties.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    checkout_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Null means a standing vacancy — no next guest booked yet.
    checkin_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: True when the next guest arrives the same day the last one leaves. The
    #: hardest turnovers to staff and the ones that price highest.
    is_same_day: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    status: Mapped[TurnoverStatus] = mapped_column(
        Enum(
            TurnoverStatus,
            name="turnover_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=TurnoverStatus.DRAFT,
        server_default=TurnoverStatus.DRAFT.value,
        index=True,
    )

    urgency: Mapped[TurnoverUrgency] = mapped_column(
        Enum(
            TurnoverUrgency,
            name="turnover_urgency",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=TurnoverUrgency.STANDARD,
        server_default=TurnoverUrgency.STANDARD.value,
        index=True,
    )

    #: What kind of clean this is. A turnover and a move-out are both "a
    #: clean" and are not the same job; a cleaner who cannot tell them apart
    #: before bidding prices one of them wrong.
    service_type: Mapped[ServiceType] = mapped_column(
        Enum(
            ServiceType,
            name="service_type",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=ServiceType.TURNOVER,
        server_default=ServiceType.TURNOVER.value,
    )

    #: The calendar feed that proposed this job, if one did. Null on anything
    #: an owner posted themselves — which is most of them, and all of them
    #: before this existed.
    source_calendar_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("property_calendars.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    #: The booking's own id in that feed (its iCalendar UID). **Identity comes
    #: from the feed, not from the dates**: a booking whose dates move is still
    #: the same booking, and without this it would become a second job while
    #: the first sat orphaned.
    external_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)
    #: When sync last wrote this row. A row with this set and `owner_edited_at`
    #: still null is a draft the feed owns and may keep up to date.
    source_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: A digest of the feed URL that proposed this job. **Kept so identity can
    #: survive its calendar being deleted**: removing a feed nulls
    #: `source_calendar_id`, and re-adding the same feed adopts the orphans
    #: rather than proposing every booking a second time — but "the same feed"
    #: has to mean something, and a matching event id on the same property does
    #: not prove it. Two different listings whose feeds reuse a UID string would
    #: otherwise hand one's jobs to the other.
    #:
    #: A digest rather than the URL because the URL is a credential (see
    #: `PropertyCalendar.url`) and this column sits on a row with
    #: cleaner-facing shapes near it. Equality is all adoption needs.
    source_feed_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: When a **person** last changed this job, as opposed to the system
    #: maintaining it. This is the answer to the only question a re-sync asks:
    #: has somebody touched this? If they have, the feed does not get to argue.
    #:
    #: **It is its own column because `updated_at` answers a different
    #: question.** `updated_at` moves for any write at all, and the read paths
    #: write: `refresh_urgency` persists a standing vacancy's climb up the
    #: ladder, so merely *looking* at a turnover list could stamp a synced draft
    #: as edited. From then on the feed could neither move its dates nor remove
    #: it when the guest cancelled — rule 2 silently switched off by a page
    #: view, with nothing failing to say so.
    owner_edited_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: Integer cents. Optional guide price the owner posts with the job.
    owner_budget_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Set when the job itself is called off for good. Not the same as an award
    #: coming undone: a cleaner backing out re-opens the turnover rather than
    #: cancelling it, and that is recorded on the award row.
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Set when a booking came undone and the job went back on the bench. It is
    #: an input to the urgency ladder, not a second author of it: once a job is
    #: being re-staffed, the time left to find somebody counts again, exactly as
    #: it does for a standing vacancy (see `app.services.urgency`).
    reopened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    property: Mapped["Property"] = relationship(back_populates="turnovers")
    bids: Mapped[list["Bid"]] = relationship(
        back_populates="turnover",
        cascade="all, delete-orphan",
    )
    #: Every award this turnover has ever had, newest first. At most one of them
    #: is live at a time — guardrail 1, backstopped by a partial unique index.
    awards: Mapped[list["Award"]] = relationship(
        back_populates="turnover",
        cascade="all, delete-orphan",
        order_by="Award.awarded_at.desc()",
    )

    # `builtins.property` spelled out: this class has a relationship *named*
    # `property`, which shadows the builtin everywhere below it in the body.
    @builtins.property
    def live_award(self) -> "Award | None":
        """The award currently in force, if any. Never a cancelled one."""
        return next((award for award in self.awards if award.cancelled_at is None), None)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Turnover {self.id} {self.status.value}/{self.urgency.value}>"
