"""A booking calendar an owner points us at, so turnovers post themselves.

**The feed proposes; linx decides.** That is the one rule everything here is
built around. An Airbnb or VRBO export is a *read-only projection* of somebody
else's system: we can read it, we cannot correct it, and it can be wrong, stale,
or briefly unreachable. So a synced booking becomes a **draft** turnover that
the owner confirms, never a live job on the bench.

The alternative — posting straight to the bench — is how a test booking or a
feed glitch turns into ten real jobs that cleaners bid on and somebody has to
cancel. An owner who wanted that can post ten jobs in a minute; an owner who did
not cannot unsend the alerts.

Three consequences of "projection, not source of truth":

- **A turnover that a person has touched is theirs.** Sync updates a draft it
  wrote and nothing else. `source_synced_at` against `updated_at` is how it
  knows: if the row changed after the last sync, somebody edited it, and the
  feed does not get to argue.
- **Disappearing from the feed is not permission to delete.** A cancelled
  booking removes an untouched draft, because nothing was staffed for it. A job
  somebody is booked on is left exactly alone and reported instead — a guest
  cancelling does not get to cancel a cleaner.
- **Identity comes from the feed, not from the schedule.** `external_ref` is the
  event's UID, so a booking whose dates move is recognised as the same booking
  rather than becoming a second job.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.property import Property


class PropertyCalendar(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "property_calendars"
    __table_args__ = (
        # The same feed twice on one property would double every booking into
        # two drafts, because identity is (calendar, event) and the two
        # calendars would be different rows.
        UniqueConstraint("property_id", "url", name="uq_property_calendars_property_url"),
    )

    property_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("properties.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    #: The .ics URL. Airbnb and VRBO both hand these out per listing, and they
    #: are **secret in the way a link is secret** — anybody holding it can read
    #: the booking dates. It is never exposed on any cleaner-facing shape.
    url: Mapped[str] = mapped_column(Text, nullable=False)

    #: What the owner calls it: "Airbnb", "VRBO", "the other listing". Free text
    #: because guessing the platform from the URL is a guess, and a wrong label
    #: on somebody's own screen is worse than the one they typed.
    label: Mapped[str] = mapped_column(
        String(80), nullable=False, default="Calendar", server_default="Calendar"
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    #: When the owner disconnected this feed — **removal is an archive, not a
    #: delete**, and that is what gives feed identity something stable to hang
    #: on.
    #:
    #: A feed's identity is not observable from outside: the export URL rotates,
    #: the event UIDs are arbitrary feed-local strings, and the one genuinely
    #: stable thing is this row's id. Deleting the row threw that away, so
    #: reconnecting found nothing of its own and proposed every booking again —
    #: one stay, two jobs. Three separate keys were tried to work around it and
    #: each had an edge at one end or the other, because they were all
    #: reconstructions of an identity that had been destroyed rather than kept.
    #:
    #: So the row survives, `turnovers.source_calendar_id` keeps pointing at it,
    #: and reconnecting the same URL reactivates *this* calendar. The unique
    #: constraint on `(property_id, url)` is what makes that findable: an
    #: archived row still holds its URL, so a second add on the same URL lands
    #: on this row rather than creating a rival.
    #:
    #: **Distinct from `is_active`, deliberately.** That is the owner's pause
    #: switch: still connected, still on the panel, not being read right now.
    #: This one means gone from the panel. Conflating them would make "pause"
    #: and "remove" the same button with two labels.
    removed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    # --- what happened last time, kept visible -----------------------------
    #
    # A sync that quietly stops working looks exactly like a calendar with no
    # bookings in it. These columns are what makes the difference legible on
    # the owner's own screen rather than in a log nobody reads.

    #: The last **attempt**, successful or not. This is what the owner's panel
    #: shows as "Last read", because an attempt that failed is still a read.
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: How many times this feed has been read successfully. **The whole of the
    #: concurrency story**, and an integer rather than a clock on purpose.
    #:
    #: Two reads of one feed can overlap, and the slower one must not commit an
    #: older snapshot over a newer one. Three rounds of review went into doing
    #: that with timestamps — which one to store, which one to compare, whether
    #: a failed read counts — and each answer produced the next question,
    #: because wall-clock comparison across two processes is the wrong
    #: primitive for "has anything happened since I looked?"
    #:
    #: This is plain optimistic concurrency instead: a reader notes the epoch
    #: before fetching, and under the lock either it is unchanged — nothing
    #: happened, commit and bump it — or it is not, and this snapshot is stale
    #: by definition. No clocks, no skew, and one value that means one thing.
    sync_epoch: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    #: Null when the last run succeeded. A sentence when it did not — shown to
    #: the owner, so it says what to do rather than naming an exception class.
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Bookings seen in the feed on the last successful run. Zero is a real
    #: answer and a suspicious one, which is why it is a number rather than a
    #: boolean.
    last_booking_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Jobs the feed no longer has a booking for, which were **kept** because
    #: somebody had already acted on them — posted, bid on, awarded.
    #:
    #: This is the number that says *a guest cancelled and a cleaner may still
    #: be coming*, and it is persisted rather than merely returned because the
    #: run that finds it is usually the unattended one. A count that only
    #: existed in the reply to a button nobody pressed is a warning the product
    #: promised and never delivered.
    last_stale_kept: Mapped[int | None] = mapped_column(Integer, nullable=True)

    property: Mapped["Property"] = relationship(back_populates="calendars")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<PropertyCalendar {self.label}>"
