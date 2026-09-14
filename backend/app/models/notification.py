"""Notifications — the record that somebody was told, or that nobody was.

Phase 5. Before this table existed, "we alerted the owner" was a log line: true
at the moment it was written and unprovable a week later, when the owner says
nobody told them. A row is the difference between an answer and a shrug.

Three parts of the shape carry the rules from CLAUDE.md:

* **`dedupe_key` is unique.** Duplicates are as bad as misses — an alert that
  arrives three times gets muted, which is the same as not sending it. The key
  is derived from the event and what it is about (a turnover, a bid, a
  recipient), never from a clock or a random value, so the *same* notification
  produces the *same* key on a retry and the database refuses the second one.
  This is guardrail 2's reasoning applied to sending rather than to charging.

* **The row is written in the same transaction as the state change**, and
  delivery happens afterwards. A notification recorded before the commit could
  describe something that then rolled back; a send with no row cannot be
  audited. So: record, commit, then send.

* **`status` distinguishes "failed" from "unknown".** A send that raised is
  `failed` with the message kept. A send whose outcome nobody knows stays
  `pending` with `attempted_at` set, visible for a human to resolve — never
  quietly marked sent.

The rendered `subject` and `body` are stored rather than re-rendered on demand,
because what matters in a dispute is what the person was actually told, not what
today's template would say.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import NotificationChannel, NotificationEvent, NotificationStatus

if TYPE_CHECKING:
    from app.models.turnover import Turnover
    from app.models.user import User


class Notification(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "notifications"
    __table_args__ = (
        # The outbox query: everything still owed, oldest first.
        Index("ix_notifications_status_created_at", "status", "created_at"),
    )

    event: Mapped[NotificationEvent] = mapped_column(
        Enum(
            NotificationEvent,
            name="notification_event",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        index=True,
    )

    #: Always a user, never a loose address. "Admin" is a role; resolving it to
    #: whoever holds that role today is the service's job, and storing the user
    #: means a changed email address does not orphan the history.
    recipient_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: The address it was actually sent to, frozen at send time. A person who
    #: later changes their email should still be able to see where it went.
    destination: Mapped[str] = mapped_column(String(320), nullable=False)

    channel: Mapped[NotificationChannel] = mapped_column(
        Enum(
            NotificationChannel,
            name="notification_channel",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=NotificationChannel.EMAIL,
        server_default=NotificationChannel.EMAIL.value,
    )

    #: What it was about, when it was about something. Null for anything that is
    #: not tied to one job.
    turnover_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("turnovers.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    #: Unique. Derived from the event and its subject, never from a clock.
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)

    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[NotificationStatus] = mapped_column(
        Enum(
            NotificationStatus,
            name="notification_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=NotificationStatus.PENDING,
        server_default=NotificationStatus.PENDING.value,
        index=True,
    )

    #: Written and committed *before* the send. A process that dies mid-send
    #: leaves a row that is visibly attempted rather than one that looks
    #: untouched — the same rule guardrail 2 applies to a Stripe call.
    attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    recipient: Mapped["User"] = relationship()
    turnover: Mapped["Turnover | None"] = relationship()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Notification {self.event.value} -> {self.destination} {self.status.value}>"
