"""Raising a dispute, and working the inbox it lands in.

**Disputes go to a human, not a bot, at v1** (CLAUDE.md). Everything here is
built to keep that literally true rather than decoratively true: there is no
severity score, no routing rule, no auto-close, and no state a dispute reaches
without a person putting it there. The only things this module decides are who
is allowed to raise one, who is allowed to work it, and who gets told — and the
last of those it hands to `app/services/notifications.py`, which owns it.

Three rules the shape depends on:

* **An award is what makes a dispute possible.** Not a *live* award, and not a
  finished job: a cleaner who backed out, a no-show, a cancellation and a
  completed clean are all disputable, because each has two people who were in a
  relationship about it. A turnover nobody ever took has no counterparty, so
  there is nothing to dispute and it refuses.
* **There is no window.** A review has one because a review is a public claim
  about somebody, and a stale one is noise. A dispute is a complaint, and a
  complaint that arrives late is still a complaint — a deadline here would
  silently close the file on the person who took three weeks to work out what
  to say.
* **Resolving requires a note.** A dispute closed with no reason is one nobody
  can argue with afterwards, which is the failure this table exists to prevent.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.award import Award
from app.models.dispute import Dispute
from app.models.enums import DisputeReason, DisputeStatus, UserRole
from app.models.property import Property
from app.models.turnover import Turnover
from app.models.user import User
from app.services import notifications


class DisputeRefused(Exception):
    """This dispute cannot be raised or changed, and the reason is sayable."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class Parties:
    """The two people a dispute about this turnover is between."""

    owner: User
    cleaner: User
    award: Award


def parties(db: Session, turnover: Turnover) -> Parties | None:
    """Who may raise a dispute about this turnover, or None if nobody may.

    Deliberately reads the **most recent award, cancelled or not**, where
    `reviews.participants` insists on a live, completed one. The difference is
    the point: the jobs most worth complaining about are the ones that went
    wrong, and a cancelled award is the record of exactly that.
    """
    award = db.execute(
        select(Award)
        .where(Award.turnover_id == turnover.id)
        .order_by(Award.awarded_at.desc())
    ).scalars().first()
    if award is None:
        return None

    prop = db.get(Property, turnover.property_id)
    if prop is None:
        return None
    owner = db.get(User, prop.owner_id)
    cleaner = db.get(User, award.cleaner_id)
    if owner is None or cleaner is None:
        return None

    return Parties(owner=owner, cleaner=cleaner, award=award)


def role_for(people: Parties, user: User) -> UserRole | None:
    """Which side of this turnover the user is on, if either."""
    if user.id == people.owner.id:
        return UserRole.OWNER
    if user.id == people.cleaner.id:
        return UserRole.CLEANER
    return None


def open_dispute_of(db: Session, turnover_id: uuid.UUID, user: User) -> Dispute | None:
    """This person's unresolved dispute on this turnover, if they have one."""
    return db.execute(
        select(Dispute).where(
            Dispute.turnover_id == turnover_id,
            Dispute.raised_by_id == user.id,
            Dispute.status != DisputeStatus.RESOLVED,
        )
    ).scalars().first()


# --------------------------------------------------------------------------
# Raising one
# --------------------------------------------------------------------------


def raise_dispute(
    db: Session,
    *,
    turnover: Turnover,
    raiser: User,
    reason: DisputeReason,
    description: str,
) -> Dispute:
    """File a complaint about this job. Commits, then delivers.

    The duplicate guard below is **a courtesy, not an invariant**, and the
    difference is worth being straight about: two simultaneous submissions could
    both pass it and create two rows. Nothing is corrupted when they do — the
    cost is one extra card in a human's queue, which a human closes. That is not
    the class of failure guardrail 1 exists for, so this deliberately does not
    take a row lock; a lock here would imply an atomicity this path does not
    need and does not have.
    """
    people = parties(db, turnover)
    if people is None:
        raise DisputeRefused(
            "Nobody was ever booked for this turnover, so there is no one to "
            "raise a dispute with. If the problem is the posting itself, "
            "cancel it instead."
        )

    role = role_for(people, raiser)
    if role is None:
        raise DisputeRefused("You were not part of this turnover.")

    existing = open_dispute_of(db, turnover.id, raiser)
    if existing is not None:
        raise DisputeRefused(
            "You already have an open dispute on this job. Somebody is looking "
            "at it — adding a second one puts you further down the queue, not "
            "further up it."
        )

    text = description.strip()
    if not text:
        raise DisputeRefused("Say what happened — an empty dispute cannot be acted on.")

    dispute = Dispute(
        turnover_id=turnover.id,
        raised_by_id=raiser.id,
        raised_by_role=role,
        reason=reason,
        description=text,
        status=DisputeStatus.OPEN,
    )
    db.add(dispute)
    db.flush()

    prop = db.get(Property, turnover.property_id)
    if prop is not None:
        notifications.dispute_raised(db, turnover, prop, dispute, raiser)

    db.commit()
    notifications.deliver_pending(db)
    return dispute


# --------------------------------------------------------------------------
# Working it — an admin, by hand, every time
# --------------------------------------------------------------------------


def acknowledge(db: Session, dispute: Dispute, admin: User) -> Dispute:
    """Mark that a person has picked this up. Sends nothing, on purpose.

    "Read" and "settled" are different facts. An admin working a backlog needs
    to tell them apart, and the person who raised it sees this state when they
    look at their own dispute — so the queue is not a void without becoming a
    mailing list.
    """
    if dispute.status is DisputeStatus.RESOLVED:
        raise DisputeRefused("This dispute is already resolved.")

    if dispute.status is DisputeStatus.OPEN:
        dispute.status = DisputeStatus.ACKNOWLEDGED
        dispute.acknowledged_at = datetime.now(timezone.utc)
        dispute.acknowledged_by_id = admin.id
        db.commit()
    return dispute


def resolve(db: Session, dispute: Dispute, admin: User, notes: str) -> Dispute:
    """Settle it, and tell both sides what was decided. Commits, then delivers.

    Both parties hear, not just the person who complained. The other side may
    be learning that a dispute existed at all — that is deliberate, and it is
    why the notification carries the admin's note rather than a status word:
    "resolved" with no explanation, to somebody who did not know they were
    being complained about, is worse than silence.

    Two admins resolving at the same instant is not guarded with a lock. The
    last write wins on the row, which is two people agreeing anyway, and the
    notification's dedupe key means the message still goes out exactly once.
    """
    if dispute.status is DisputeStatus.RESOLVED:
        raise DisputeRefused("This dispute is already resolved.")

    text = notes.strip()
    if not text:
        raise DisputeRefused(
            "A resolution needs a reason. A dispute closed with no explanation "
            "is one nobody can argue with, which is the thing this is for."
        )

    dispute.status = DisputeStatus.RESOLVED
    dispute.resolved_at = datetime.now(timezone.utc)
    dispute.resolved_by_id = admin.id
    dispute.resolution_notes = text

    turnover = db.get(Turnover, dispute.turnover_id)
    people = parties(db, turnover) if turnover is not None else None
    prop = db.get(Property, turnover.property_id) if turnover is not None else None
    if turnover is not None and people is not None and prop is not None:
        notifications.dispute_resolved(db, turnover, prop, dispute, people)

    db.commit()
    notifications.deliver_pending(db)
    return dispute


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def inbox(
    db: Session, *, include_resolved: bool = False, limit: int = 100, offset: int = 0
) -> list[Dispute]:
    """The admin queue. **Oldest first, and open ones first.**

    Newest-first is the wrong default for a queue somebody works: it buries the
    complaint that has been waiting longest under the one that arrived this
    morning, and the one waiting longest is the one about to become a phone
    call.
    """
    stmt = select(Dispute)
    if not include_resolved:
        stmt = stmt.where(Dispute.status != DisputeStatus.RESOLVED)

    return list(
        db.execute(
            stmt.order_by(Dispute.resolved_at.is_not(None), Dispute.created_at)
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )


def raised_by(db: Session, user: User) -> list[Dispute]:
    """Somebody's own disputes, newest first — a person reads their own list
    the other way round from how an admin works a queue."""
    return list(
        db.execute(
            select(Dispute)
            .where(Dispute.raised_by_id == user.id)
            .order_by(Dispute.created_at.desc())
        )
        .scalars()
        .all()
    )


def open_count(db: Session) -> int:
    """How many need a person. The number the console leads with."""
    return len(
        db.execute(
            select(Dispute.id).where(Dispute.status != DisputeStatus.RESOLVED)
        )
        .scalars()
        .all()
    )
