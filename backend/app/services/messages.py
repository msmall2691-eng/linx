"""Owner and booked cleaner, talking to each other about one job.

**This is the feature that reopened a v1 scope decision**, so the reasoning is
written here rather than assumed. "In-app messaging is out of scope; email and
SMS are enough at this size" was right about *notifications* — a one-way alert
does not need a thread — and wrong about the question a cleaner standing at a
locked gate actually has. That question previously went to a phone number this
product deliberately does not hand out, or it went unasked and somebody guessed.

Everything else here follows from the privacy boundary, which this feature does
**not** move:

1. **A thread belongs to a live award.** A cleaner who has bid has not been
   hired, so there is no channel before an award and none after it ends — the
   same line the street address and the access notes follow, decided in the
   same place rather than re-derived.
2. **Nobody learns a name they did not already know.** The owner already sees
   the cleaner's; the cleaner does not see the owner's, and a thread is the
   easiest place in this product to leak one. `visible_sender` is the single
   author of what a reader is shown, so a new screen cannot invent its own
   labelling and widen the boundary by accident.
3. **Nothing is editable or deletable.** What was said is what a dispute is
   argued from. A thread one side can quietly rewrite once the other has read
   it is worth less than no thread at all — the same reasoning as a review that
   cannot be edited and an award that is cancelled rather than deleted.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.award import Award
from app.models.enums import UserRole
from app.models.message import JobMessage
from app.models.property import Property
from app.models.turnover import Turnover
from app.models.user import User
from app.services import awards as awards_service
from app.services import notifications

#: The longest a single message may be. Not a policy about how much somebody
#: may say — they can send another — but a bound on a text column that arrives
#: from a form and is rendered into an email.
MAX_BODY = 4000


class MessageRefused(Exception):
    """Why a message could not be sent, in words for the person sending it."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def live_award_for(
    db: Session, turnover_id: uuid.UUID, user: User
) -> Award | None:
    """The booking this person may talk through, or None.

    **Live only**, and that is the whole access rule. `disputes.award_for`
    deliberately reads cancelled awards too, because the jobs most worth
    complaining about are the ones that did not finish; a conversation is the
    opposite case. Once nobody is booked there is nobody to reach, and a thread
    that stayed open would be a channel to a stranger's house outliving the
    reason it was opened.

    Returns None rather than raising for either "not yours" or "not there", so
    every caller answers 404 to both — a refusal that tells them apart confirms
    the id exists.
    """
    turnover = db.get(Turnover, turnover_id)
    if turnover is None:
        return None
    prop = db.get(Property, turnover.property_id)
    if prop is None:
        return None

    award = db.execute(
        select(Award).where(
            Award.turnover_id == turnover_id,
            Award.cancelled_at.is_(None),
        )
    ).scalars().first()
    if award is None:
        return None

    if user.id == prop.owner_id or user.id == award.cleaner_id:
        return award
    return None


def visible_sender(db: Session, message: JobMessage, *, reader: User) -> str:
    """What this reader is told about who sent it. **One author, on purpose.**

    The asymmetry is the existing privacy boundary, not a new one: an owner
    already knows which cleaner they booked, and a cleaner is never told whose
    house it is. Left to each screen, the cleaner's thread would show a name
    the rest of the product goes out of its way to withhold, and nothing would
    fail — the messages would render perfectly.
    """
    if message.sender_id == reader.id:
        return "You"
    sender = db.get(User, message.sender_id)
    if sender is None:  # pragma: no cover - the foreign key prevents it
        return "Them"
    if sender.role is UserRole.OWNER:
        return "The owner"
    return sender.full_name


def history(db: Session, award: Award) -> list[JobMessage]:
    """The whole thread, oldest first. Short enough not to page at this size."""
    return list(
        db.execute(
            select(JobMessage)
            .where(JobMessage.award_id == award.id)
            .order_by(JobMessage.created_at, JobMessage.id)
        )
        .scalars()
        .all()
    )


def post(
    db: Session, *, turnover: Turnover, award: Award, sender: User, body: str
) -> JobMessage:
    """Say something. **Commits, and tells the other side.**

    A message nobody is told about is a message nobody reads: there is no push
    channel in this product, so the notification is the entire delivery
    mechanism rather than a convenience on top of one. It is keyed on the
    message's own id, because each message is a separate thing to be told about
    — unlike every other event here, where the key exists to stop one
    transition being announced twice.
    """
    text = body.strip()
    if not text:
        raise MessageRefused("There is nothing to send.")
    if len(text) > MAX_BODY:
        raise MessageRefused(
            f"That message is longer than the {MAX_BODY} characters one can "
            "hold. Send it in two."
        )
    if award.completed_at is not None and turnover.status not in (
        awards_service.LIVE_BOOKING_STATUSES
    ):
        # A finished job keeps its thread readable — it is the record — but
        # reopening a conversation on it is what a dispute is for, and a
        # message the other side has no screen prompting them to answer is a
        # message into a void.
        raise MessageRefused(
            "This job is finished. If something went wrong with it, raise it "
            "as a dispute so somebody actually looks at it."
        )

    message = JobMessage(award_id=award.id, sender_id=sender.id, body=text)
    db.add(message)
    # Flushed so the notification can be keyed on the message's own id, the way
    # every other key here is built from a row that already exists.
    db.flush()

    prop = db.execute(
        select(Property).where(Property.id == turnover.property_id)
    ).scalar_one()
    notifications.message_received(
        db, turnover=turnover, prop=prop, award=award, message=message, sender=sender
    )

    db.commit()
    notifications.deliver_pending(db)
    db.refresh(message)
    return message
