"""Awarding a turnover, and unwinding an award that comes undone.

**This module is where guardrail 1 lives.** Accepting a bid is the one action in
the product that hands a single job to exactly one party, and the whole reason
the rule exists is that the obvious implementation — read the turnover, see it
is not awarded, then write — is wrong under concurrency: two requests both read
"not awarded" before either writes, and the owner ends up promising one cleaning
to two people.

So the order is fixed, and it is the caller's job not to break it:

1. `lock_turnover` takes `SELECT ... FOR UPDATE` on the turnover row **before
   anything is checked about it**, including who owns it.
2. Everything that follows — the status check, the live-award check, the bid
   check, the insert — happens while that lock is held.
3. One commit at the end releases it.

There is no commit in the middle, which matters: `expire_on_commit=False` means
an object read before a commit keeps its old values afterwards, so a check made
against a pre-commit copy is a check against the past. If a future change has to
commit partway through, it must re-read the row fresh under the lock, not carry
the in-memory copy across.

The partial unique index on `awards` is the backstop, not the mechanism. If a
route ever reaches the insert without the lock, the database raises instead of
double-booking — the right failure, and still a bug.

The unwinding half is the no-show / late-cancellation path. It is deliberately
the same shape for all three ways a booking ends (the cleaner backs out, the
owner calls it off, the cleaner never turns up) so that no route can quietly
skip the parts that matter: the award is cancelled rather than deleted, the job
goes back on the bench where it can be re-staffed, and the people affected are
told.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.award import Award
from app.models.bid import Bid
from app.models.cleaner_profile import CleanerProfile
from app.models.enums import BidStatus, TurnoverStatus
from app.models.property import Property
from app.models.turnover import Turnover
from app.models.user import User
from app.services import notifications, vetting
from app.services.turnovers import apply_derived_fields

#: A cancellation inside this window of checkout is *late*: too close for the
#: owner to comfortably find someone else. It does not change what happens —
#: every cancellation re-posts the job and alerts the owner and an admin — it
#: changes how loudly it reads, and it is the number the no-show policy is
#: written against. One place, so the policy and the alert cannot disagree.
LATE_CANCELLATION_WITHIN = timedelta(hours=48)


class AwardConflict(Exception):
    """The action cannot be taken in the turnover's current state."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class BidNotFound(Exception):
    """No such bid on this turnover."""


def lock_turnover(db: Session, turnover_id: uuid.UUID) -> Turnover | None:
    """Take the row lock. Call this *before* reading anything about the row.

    Deliberately a bare select with no eager loading: `FOR UPDATE` cannot be
    applied across the outer join a `joinedload` would add. Anything else the
    caller needs is read afterwards, inside the lock.
    """
    return db.execute(
        select(Turnover).where(Turnover.id == turnover_id).with_for_update()
    ).scalar_one_or_none()


def live_award(db: Session, turnover_id: uuid.UUID) -> Award | None:
    """The award in force for this turnover, read from the database.

    A query rather than `turnover.awards`, because the collection may have been
    loaded before the lock was taken and would then answer from before it.
    """
    return db.execute(
        select(Award).where(Award.turnover_id == turnover_id, Award.cancelled_at.is_(None))
    ).scalar_one_or_none()


def owns_turnover(db: Session, turnover: Turnover, owner: User) -> bool:
    """Whether this owner's property the turnover belongs to. 404, not 403."""
    return (
        db.execute(
            select(Property.id).where(
                Property.id == turnover.property_id, Property.owner_id == owner.id
            )
        ).scalar_one_or_none()
        is not None
    )


def is_late(turnover: Turnover, *, now: datetime | None = None) -> bool:
    """True when checkout is inside the late-cancellation window, or past it."""
    reference = now or datetime.now(timezone.utc)
    return turnover.checkout_at - reference < LATE_CANCELLATION_WITHIN


def accept_bid(db: Session, *, turnover: Turnover, bid_id: uuid.UUID) -> Award:
    """Award the turnover to one bid. **The row must already be locked.**

    Every check below runs inside that lock, and the single commit at the end is
    what releases it. A second request that got as far as the lock finds the
    turnover already awarded and is refused — that is the guardrail working, and
    it is the reason none of this is split across two transactions.
    """
    if turnover.status is not TurnoverStatus.OPEN:
        raise AwardConflict(
            f"This turnover is {turnover.status.value} and is not taking bids."
        )
    if live_award(db, turnover.id) is not None:
        raise AwardConflict("This turnover has already been awarded.")

    bid = db.execute(
        select(Bid).where(Bid.id == bid_id, Bid.turnover_id == turnover.id)
    ).scalar_one_or_none()
    if bid is None:
        raise BidNotFound
    if bid.status is not BidStatus.SUBMITTED:
        raise AwardConflict(f"That bid is {bid.status.value} and cannot be accepted.")

    # The same gate the board uses, read from the same place. Clearance can
    # lapse between a bid and an accept — a background check that came back
    # rejected last week must not be able to walk into a house today.
    profile = db.execute(
        select(CleanerProfile).where(CleanerProfile.user_id == bid.cleaner_id)
    ).scalar_one_or_none()
    if profile is None or not vetting.evaluate(profile).can_take_jobs:
        raise AwardConflict(
            "This cleaner is no longer cleared to take jobs, so their bid cannot be accepted."
        )

    award = Award(
        turnover_id=turnover.id,
        cleaner_id=bid.cleaner_id,
        bid_id=bid.id,
        agreed_price_cents=bid.price_cents,
    )
    db.add(award)

    bid.status = BidStatus.ACCEPTED
    # Everyone else hears no — read before the update, because they have to be
    # told, and after the update there is nothing left to identify them by.
    losing_bids = list(
        db.execute(
            select(Bid).where(
                Bid.turnover_id == turnover.id,
                Bid.id != bid.id,
                Bid.status == BidStatus.SUBMITTED,
            )
        )
        .scalars()
        .all()
    )
    # Leaving them `submitted` against an awarded job would show a cleaner a bid
    # that is still pending on work already gone.
    db.execute(
        update(Bid)
        .where(
            Bid.turnover_id == turnover.id,
            Bid.id != bid.id,
            Bid.status == BidStatus.SUBMITTED,
        )
        .values(status=BidStatus.DECLINED)
        .execution_options(synchronize_session=False),
    )

    turnover.status = TurnoverStatus.AWARDED

    # Queued inside this transaction, alongside the award itself: the message
    # and the fact it describes land together or not at all. The flush is so
    # the award has an id to key the notification on.
    db.flush()
    prop = db.execute(
        select(Property).where(Property.id == turnover.property_id)
    ).scalar_one()
    notifications.bid_accepted(db, award, turnover, prop)
    notifications.bids_declined(db, turnover, prop, losing_bids)

    db.commit()

    notifications.deliver_pending(db)
    return award


def cancel_award(
    db: Session,
    *,
    turnover: Turnover,
    award: Award,
    actor: User,
    reason: str | None,
    no_show: bool = False,
    reopen: bool,
) -> Award:
    """Unwind a live award. **The turnover row must already be locked.**

    `reopen` decides what happens to the job itself: back on the bench when the
    owner still needs it cleaned (a cleaner backing out, a no-show), or closed
    for good when the owner is calling the whole thing off.

    The award row is cancelled, never deleted. Who was booked and who backed out
    is exactly the history a dispute is argued from, and the partial unique index
    means keeping it costs nothing — the next award is still the only live one.
    """
    now = datetime.now(timezone.utc)
    late = is_late(turnover, now=now)

    award.cancelled_at = now
    award.cancellation_reason = reason
    award.cancelled_by_id = actor.id
    award.was_no_show = no_show

    if award.bid_id is not None:
        bid = db.get(Bid, award.bid_id)
        if bid is not None:
            # The offer has to come off `accepted` either way: an accepted bid
            # is one the cleaner cannot edit, which would leave them unable to
            # re-bid on a job that is back on the bench.
            bid.status = (
                BidStatus.WITHDRAWN if actor.id == award.cleaner_id else BidStatus.DECLINED
            )

    if reopen:
        turnover.status = TurnoverStatus.OPEN
        turnover.reopened_at = now
    else:
        turnover.status = TurnoverStatus.CANCELLED
        turnover.cancelled_at = now
        turnover.cancellation_reason = reason

    # The ladder is re-derived rather than set: `reopened_at` is an input to the
    # one function that decides urgency, not a second opinion about it.
    apply_derived_fields(turnover, now=now)

    # Both sides and an admin, queued in the same transaction as the
    # cancellation itself. Phase 4 emitted this after the commit, which left a
    # gap where a crash lost the alert entirely; the notification row closes it.
    prop = db.execute(
        select(Property).where(Property.id == turnover.property_id)
    ).scalar_one()
    notifications.award_cancelled(
        db,
        turnover=turnover,
        prop=prop,
        award=award,
        actor=actor,
        no_show=no_show,
        late=late,
    )

    db.commit()

    notifications.deliver_pending(db)
    return award
