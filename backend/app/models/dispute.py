"""A disagreement between two people that a human decides.

**Disputes go to a human inbox, not a bot, at v1** (CLAUDE.md). This table is
what makes that true rather than aspirational: before phase 8 there was no way
to raise one at all, so "a human handles it" meant "somebody emails Meg, if they
can work out how".

Three things about the shape are deliberate:

- **It hangs off a turnover, not off an award.** A dispute about a job whose
  award was cancelled is still a dispute — arguably the most important kind,
  since somebody backed out. Keying it to the live award would lose exactly the
  cases worth reading.
- **Nothing here is automated.** There is no severity score, no auto-close, no
  routing rule. `resolution_notes` is prose a person wrote, because the outcomes
  at this size are not an enumerable set and a column of them would be a policy
  nobody agreed to.
- **It is never deleted.** Same reasoning as a cancelled award: who complained
  about what, and what was decided, is the history the next argument is had
  from. Resolution is a status and a note, not a disappearance.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import DisputeReason, DisputeStatus, UserRole

if TYPE_CHECKING:
    from app.models.award import Award
    from app.models.turnover import Turnover
    from app.models.user import User


def _pg_enum(enum_type: type, name: str) -> Enum:
    return Enum(enum_type, name=name, values_callable=lambda e: [m.value for m in e])


class Dispute(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "disputes"
    __table_args__ = (
        # The admin inbox reads open ones first and by age. Oldest-first on an
        # unbounded queue is the ordering that stops a complaint being buried
        # by newer noise, so it is worth an index rather than a sort of
        # everything ever raised.
        Index("ix_disputes_status_created_at", "status", "created_at"),
    )

    turnover_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("turnovers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    #: **Which booking this is about, frozen at filing time.** A turnover can
    #: carry several awards over its life: a cancellation re-posts it to the
    #: bench and the next accept writes a second `Award`. Read back through
    #: "the award on this turnover" — which is what this used to do — every
    #: existing dispute silently re-pointed at the replacement cleaner, so the
    #: console showed an uninvolved person's contact details, resolving
    #: notified them about somebody else's complaint, and the cleaner who
    #: actually raised it got a 404 on their own dispute.
    award_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("awards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    #: Who raised it. Deliberately not unique per turnover: both sides can
    #: raise one about the same job, and a second complaint from the same
    #: person weeks later is a new fact rather than an edit of the old one.
    raised_by_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: Which side of the turnover they were on when they raised it. Stored
    #: rather than re-derived, because an award can be cancelled afterwards and
    #: the question "who was complaining" must not change answer later.
    raised_by_role: Mapped[UserRole] = mapped_column(
        _pg_enum(UserRole, "user_role"), nullable=False
    )

    reason: Mapped[DisputeReason] = mapped_column(
        _pg_enum(DisputeReason, "dispute_reason"), nullable=False
    )
    #: What happened, in their words. The category is for triage; this is the
    #: thing a person actually reads.
    description: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[DisputeStatus] = mapped_column(
        _pg_enum(DisputeStatus, "dispute_status"),
        nullable=False,
        default=DisputeStatus.OPEN,
        server_default=DisputeStatus.OPEN.value,
    )

    #: Set when an admin picks it up. Separate from resolution, because "read"
    #: and "settled" are different facts and an admin working a backlog needs
    #: to tell them apart.
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    acknowledged_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: What was decided and why, written by the admin who decided it. Required
    #: to resolve — a dispute closed with no reason is one nobody can argue
    #: with, which is the failure this whole table exists to prevent.
    resolution_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    turnover: Mapped["Turnover"] = relationship()
    award: Mapped["Award"] = relationship()
    raised_by: Mapped["User"] = relationship(foreign_keys=[raised_by_id])

    @property
    def is_open(self) -> bool:
        """Still needs a person. Both pre-resolution states count."""
        return self.status is not DisputeStatus.RESOLVED

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Dispute {self.reason.value} {self.status.value}>"
