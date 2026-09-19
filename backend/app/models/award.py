"""Awards — one turnover goes to exactly one cleaner at a time.

**Guardrail 1 governs writes to this table.** The partial unique index below is
the database's backstop, not the mechanism: the mechanism is
`SELECT ... FOR UPDATE` on the `Turnover` row taken *before* checking whether it
is already awarded, with the check and the insert inside that same lock and
transaction (see CLAUDE.md, and `app.services.awards`). The index turns a missed
lock into a loud error instead of a second award, which is the right failure —
but a route that relies on catching the constraint violation has already lost
the race it was supposed to prevent.

**Why the index is partial rather than a plain unique column.** A cleaner who
backs out — or does not show up — re-opens the turnover to the bench, and the
next cleaner to be accepted needs an award row of their own. The invariant that
actually matters is *at most one **live** award per turnover*, so that is what
the index says: unique on `turnover_id` `WHERE cancelled_at IS NULL`. Cancelled
awards stay, because who was booked and who backed out is exactly the history a
dispute is argued from; deleting the row to free the constraint would erase it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.bid import Bid
    from app.models.turnover import Turnover
    from app.models.user import User


class Award(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "awards"
    __table_args__ = (
        CheckConstraint("agreed_price_cents > 0", name="agreed_price_positive"),
        # Guardrail 1's backstop: one live award per turnover, forever.
        Index(
            "uq_awards_live_turnover",
            "turnover_id",
            unique=True,
            postgresql_where=text("cancelled_at IS NULL"),
        ),
    )

    turnover_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("turnovers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    cleaner_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    #: The bid that won, kept for the audit trail.
    bid_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("bids.id", ondelete="SET NULL"),
        nullable=True,
    )

    #: Integer cents, frozen at accept time — the bid may change later, this
    #: does not. This id is also the stable seed for the Stripe idempotency key
    #: in phase 6 (guardrail 2).
    agreed_price_cents: Mapped[int] = mapped_column(Integer, nullable=False)

    awarded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    #: The cleaner says they are on their way. **The only one of these three
    #: that is about the future**, which is what makes it worth a column and a
    #: notification: `started_at` and `completed_at` report what has already
    #: happened, and an owner on the morning of a turnover is asking whether
    #: anybody is coming.
    #:
    #: It is a timestamp somebody wrote by pressing a button, and deliberately
    #: **not a position**. Continuous location on an independent contractor is a
    #: different product with its own consent, retention and disclosure
    #: questions; a coordinate column added alongside this one is how that
    #: product arrives without any of them being asked.
    en_route_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: How far the cleaner's phone said it was from the property when they
    #: tapped arrived, in metres. **A scalar against a point the owner already
    #: knows, not a position** — it describes a ring rather than a place and
    #: cannot be replayed into a trail. The reading itself is used and thrown
    #: away.
    #:
    #: Null means no conclusion was available, which is a real and common
    #: answer: permission refused, no GPS, a desktop browser, or a property
    #: with no coordinates. See `awards.arrival_check`, which has three
    #: outcomes rather than two for exactly that reason.
    arrival_distance_m: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: What the fix claimed about itself, in metres. **This is what makes the
    #: distance readable rather than merely present**: a browser falling back
    #: to IP geolocation returns something accurate to tens of kilometres, and
    #: such a fix landing inside the radius is not evidence of anything.
    #:
    #: **None of this is proof.** The coordinate came from the cleaner's own
    #: browser and can be fabricated by anyone who wants to. It is worth
    #: writing here because the failure mode is not a bug — it is somebody
    #: treating a green tick as evidence in a dispute it cannot settle.
    arrival_accuracy_m: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: The cleaner says they are on site. Nothing hangs off it but the screen —
    #: it exists so "started" and "finished" are two facts rather than one.
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: The cleaner says the job is done. **This is what money hangs off**: the
    #: owner is charged for a completed job, not a booked one, so the refund
    #: path handles disputes rather than every ordinary cancellation.
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: Set when the booking comes undone — the cleaner cancels, the owner calls
    #: it off, or the cleaner does not turn up. Null means the award is live,
    #: which is what the partial unique index above keys on.
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Who ended it. A cleaner backing out and an owner calling the job off have
    #: different consequences, and "the row says cancelled" does not say which.
    cancelled_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    #: A no-show is not a cancellation. Nobody told anyone; the owner found out
    #: by the house not being cleaned. Flagged separately because the policy
    #: response differs, and because it is what an admin needs to see.
    was_no_show: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    turnover: Mapped["Turnover"] = relationship(back_populates="awards")
    cleaner: Mapped["User"] = relationship(foreign_keys=[cleaner_id])
    cancelled_by: Mapped["User | None"] = relationship(foreign_keys=[cancelled_by_id])
    bid: Mapped["Bid | None"] = relationship()

    @property
    def is_live(self) -> bool:
        return self.cancelled_at is None

    @property
    def cleaner_name(self) -> str:
        """Who is booked, for the owner's screen. An id is not a person."""
        return self.cleaner.full_name

    @property
    def arrival_check(self) -> str:
        """`confirmed`, `away` or `unchecked` — **asked, never decided here.**

        A property on the model so the owner's `AwardOut` can read it by
        attribute, but the answer comes from `awards.arrival_check`, which is
        its one author. Re-implementing the threshold here would put a second
        opinion on it in the place most likely to be read and least likely to
        be changed when the first one moves.

        Imported inside the call because the service imports this module.
        """
        from app.services.awards import arrival_check

        return arrival_check(self)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        state = "live" if self.is_live else "cancelled"
        return f"<Award turnover={self.turnover_id} cleaner={self.cleaner_id} {state}>"
