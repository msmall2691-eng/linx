"""Messages between an owner and the cleaner booked on their job.

**In-app messaging was out of scope for v1, and that line was right until the
product grew the thing it was wrong about.** The reasoning was that email and
SMS are enough at this size, and for *notifications* they are: a one-way alert
does not need a thread. What email cannot do is let a cleaner ask "which side
is the bin store on" and have the answer attached to the job — so the question
went to a phone number the product deliberately does not hand out, or it went
unasked and somebody guessed.

Three things about the shape follow from the privacy boundary rather than from
convenience:

- **A thread belongs to a live award, not to a turnover.** Bidding is not a
  relationship: a cleaner who has bid has not been hired, and a message box on
  the board would be a channel to somebody's house before anybody agreed to it.
  Access follows the live award exactly as the address and the access notes do,
  and ends when the booking does.
- **Nobody's identity is widened by it.** The owner already sees the cleaner's
  name; the cleaner does not see the owner's, and a thread is the easiest place
  in the product to leak one. So a message is labelled for whoever is reading
  it, by `messages.visible_sender`, and the cleaner's copy says "the owner".
- **Messages are not editable and not deletable.** Same reasoning as a review
  and a cancelled award: what was said is what a dispute is argued from, and a
  thread somebody can quietly rewrite is worth less than no thread at all.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.award import Award
    from app.models.user import User


class JobMessage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "job_messages"
    __table_args__ = (
        # The only query there is: one thread, oldest first.
        Index("ix_job_messages_award_created_at", "award_id", "created_at"),
    )

    #: **The award, not the turnover.** A turnover can be booked more than once
    #: — a cleaner backs out and it is re-awarded — and one thread spanning
    #: both would hand the replacement a conversation they were never part of.
    award_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("awards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    sender_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    #: What they said. No attachments at v1: a photo channel into somebody's
    #: house is its own decision about storage, retention and what happens when
    #: the wrong thing is sent, and none of those are answered by a column.
    body: Mapped[str] = mapped_column(Text, nullable=False)

    award: Mapped["Award"] = relationship()
    sender: Mapped["User"] = relationship()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<JobMessage {self.id}>"
