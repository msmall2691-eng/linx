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

from sqlalchemy import func, select
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


def disputable_awards(db: Session, turnover: Turnover, user: User) -> list[Award]:
    """Every booking on this turnover **this person** is a party to, newest first.

    Not "the award on this turnover" — that is a question with a different
    answer next week. A turnover can carry several awards over its life: a
    cancellation re-posts it to the bench (`awards.py` sets it back to `open`)
    and the next accept writes a second row. So the party is resolved per
    person:

    * a **cleaner** is a party to their own awards, and to nobody else's — a
      cleaner whose booking was cancelled is still party to the job that went
      wrong, which is exactly the one worth complaining about. They can hold
      more than one: backing out and later winning the re-posted job is two
      awards, both theirs;
    * the **owner** is a party to all of them.

    Reading the newest award for everybody, which is what this did first, had
    two faces of one bug. A cleaner whose award had been superseded was told
    "you were not part of this turnover" about a job they had been booked for
    and lost; and every dispute already filed quietly changed which cleaner it
    was about.
    """
    prop = db.get(Property, turnover.property_id)
    if prop is None:
        return []

    awards = list(
        db.execute(
            select(Award)
            .where(Award.turnover_id == turnover.id)
            # `id` breaks the tie: two awards can share a timestamp, and an
            # ordering that is not total picks a different row on a different
            # day.
            .order_by(Award.awarded_at.desc(), Award.id.desc())
        ).scalars().all()
    )

    if prop.owner_id == user.id:
        return awards
    return [award for award in awards if award.cleaner_id == user.id]


def award_under_dispute(
    db: Session,
    turnover: Turnover,
    user: User,
    award_id: uuid.UUID | None = None,
) -> Award:
    """Which booking this complaint is about. **Refuses rather than guessing.**

    Binding the newest award is right exactly when there is only one to pick,
    and silently wrong otherwise — which is the second half of the bug that
    `dispute.award_id` fixed the first half of. Freezing the award at filing
    time stops a dispute *changing* who it is about; it does not make an
    inferred choice correct in the first place. An owner whose cleaner
    cancelled and whose job was re-awarded before they got round to
    complaining would have their complaint filed against the replacement — the
    person who has done nothing — and the console and the resolution
    notification would both name them. The same happens to a cleaner who
    cancelled, re-bid and was booked again: two awards, both theirs, and only
    they know which one went wrong.

    So the API takes an `award_id` and this refuses without one when there is
    a real choice to make. That is the house rule from `service_type_for`: a
    mismatch is refused, never corrected, because somebody who is told what
    they asked for was received can check it and somebody who is not, cannot.
    """
    candidates = disputable_awards(db, turnover, user)
    if not candidates:
        raise DisputeRefused(
            "Nobody was ever booked for this turnover, so there is no one to "
            "raise a dispute with. If the problem is the posting itself, "
            "cancel it instead."
        )

    if award_id is not None:
        chosen = next((a for a in candidates if a.id == award_id), None)
        if chosen is None:
            # Not "that award is not yours" — the 404 rule again: a refusal
            # that distinguishes "exists but not yours" from "does not exist"
            # confirms the id.
            raise DisputeRefused("That booking is not one of yours on this job.")
        return chosen

    if len(candidates) == 1:
        return candidates[0]

    raise DisputeRefused(
        "This job has been booked more than once. Say which booking you are "
        "complaining about — picking the most recent would file your "
        "complaint against whoever holds the job today, who may have had "
        "nothing to do with it."
    )


def parties_of(db: Session, award: Award) -> Parties | None:
    """The two people one specific award is between.

    Deliberately reads an award **cancelled or not**, where
    `reviews.participants` insists on a live, completed one. The difference is
    the point: the jobs most worth complaining about are the ones that went
    wrong, and a cancelled award is the record of exactly that.
    """
    turnover = db.get(Turnover, award.turnover_id)
    if turnover is None:
        return None
    prop = db.get(Property, turnover.property_id)
    if prop is None:
        return None
    owner = db.get(User, prop.owner_id)
    cleaner = db.get(User, award.cleaner_id)
    if owner is None or cleaner is None:
        return None

    return Parties(owner=owner, cleaner=cleaner, award=award)


def parties_of_dispute(db: Session, dispute: Dispute) -> Parties | None:
    """The two people a filed dispute is between — from **its own** award.

    The single reader of `dispute.award_id`, so nothing anywhere re-derives
    "which booking was this about" from the turnover's current state.
    """
    award = db.get(Award, dispute.award_id)
    return None if award is None else parties_of(db, award)




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
    award_id: uuid.UUID | None = None,
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
    award = award_under_dispute(db, turnover, raiser, award_id)
    people = parties_of(db, award)
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
        # Frozen here, and read back from here everywhere else.
        award_id=award.id,
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


def _claim(db: Session, dispute: Dispute) -> Dispute:
    """Lock the row and re-read it, before checking anything about it.

    **Guardrail 1's shape, applied to a state transition rather than an
    award.** Raising a dispute needs no lock — two rows in a queue is one extra
    card a human closes — but *working* one does, because two admins with the
    same inbox open is the ordinary case rather than a race nobody hits.

    Unlocked, each request checked a status it had loaded independently, and
    two interleavings were reachable. An acknowledge that committed after a
    resolve wrote `acknowledged` back over `resolved` while leaving
    `resolved_at` and the note populated — a row that says nobody has settled
    it, carrying a settlement, after both parties were told it was settled. And
    two resolves both passed the check, so the row kept the *last* admin's note
    while the dedupe key had already queued and sent the *first* one: the
    record a dispute is argued from later disagreeing with the message the
    people involved actually received.

    That second one is the reason this is not merely tidiness. An earlier
    version of this module argued the opposite in a docstring — "the last write
    wins, which is two people agreeing anyway, and the dedupe key means the
    message still goes out exactly once" — and both halves were wrong: they are
    not agreeing, and the message that goes out is not the one on the row.

    `populate_existing` is not optional. A locking `SELECT` takes the lock and
    still hands back the instance already in the session's identity map, with
    its old attribute values — so without it the row is locked and then read
    stale, which is the whole failure this exists to stop (CLAUDE.md says the
    same about `calendars._claim`).
    """
    locked = db.execute(
        select(Dispute)
        .where(Dispute.id == dispute.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalars().first()
    if locked is None:
        raise DisputeRefused("This dispute no longer exists.")
    return locked


def acknowledge(db: Session, dispute: Dispute, admin: User) -> Dispute:
    """Mark that a person has picked this up. Sends nothing, on purpose.

    "Read" and "settled" are different facts. An admin working a backlog needs
    to tell them apart, and the person who raised it sees this state when they
    look at their own dispute — so the queue is not a void without becoming a
    mailing list.
    """
    dispute = _claim(db, dispute)
    if dispute.status is DisputeStatus.RESOLVED:
        db.commit()  # release the lock; nothing changed
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

    **Serialised on the row** — see `_claim`. The note that is stored and the
    note that is sent have to be the same words, and without the lock they
    were not: two admins both passed the already-resolved check, the row kept
    the last one's note, and the dedupe key had already sent the first one's.
    """
    dispute = _claim(db, dispute)
    if dispute.status is DisputeStatus.RESOLVED:
        db.commit()  # release the lock; nothing changed
        raise DisputeRefused("This dispute is already resolved.")

    text = notes.strip()
    if not text:
        db.commit()  # release the lock; nothing changed
        raise DisputeRefused(
            "A resolution needs a reason. A dispute closed with no explanation "
            "is one nobody can argue with, which is the thing this is for."
        )

    dispute.status = DisputeStatus.RESOLVED
    dispute.resolved_at = datetime.now(timezone.utc)
    dispute.resolved_by_id = admin.id
    dispute.resolution_notes = text

    turnover = db.get(Turnover, dispute.turnover_id)
    # **The dispute's own award**, never the turnover's latest. Recomputed here
    # a resolution notified whichever cleaner happened to hold the job today
    # about a complaint that was not theirs, and never reached the one who
    # raised it.
    people = parties_of_dispute(db, dispute)
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
    """How many need a person. The number the console leads with.

    Counted in the database rather than by loading the ids and measuring the
    list: this runs on every console load, and a queue that is big enough to
    matter is exactly the one that must not be dragged into Python to be
    counted.
    """
    return db.execute(
        select(func.count())
        .select_from(Dispute)
        .where(Dispute.status != DisputeStatus.RESOLVED)
    ).scalar_one()
