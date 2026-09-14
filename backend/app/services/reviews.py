"""Mutual, delayed-reveal reviews — and the one place that decides visibility.

**Nothing is visible until both sides have written, or the window has passed.**
That is the entire design, and it exists because the obvious alternative has a
known failure mode: a review that appears the moment it lands rewards getting in
first with a bad one to suppress what the other side would have said. Both
people then write defensively, and the ratings stop meaning anything.

So the rule is structural rather than a matter of etiquette:

* Writing a review tells you nothing about the other side's. The submit
  response does not carry it, the read endpoint does not return it, and
  `visible_at` is null until the condition is met.
* When the second review lands, **both** are revealed in the same transaction.
  Revealing one without the other would be the thing the delay prevents.
* If only one side ever writes, it is revealed anyway after
  `REVIEW_REVEAL_AFTER_DAYS` — silence must not be a veto, or the way to bury a
  bad review becomes refusing to answer it.
* **And when that happens, the silent side's window has closed.** Otherwise
  stalling beats reviewing honestly: wait out the timeout, read theirs, then
  write yours knowing exactly what it must answer — with no reply possible,
  since there are no edits. The rule the whole module protects is therefore the
  stronger one: *no review is ever written by somebody who has seen the other
  side's.* The delay is how that holds when both write; the closed window is how
  it holds when only one does.

`visible_at` is the only flag. There is deliberately no second boolean that
could disagree with it, and no code path anywhere else sets it: everything that
reveals goes through `reveal_pair` below. A review whose `visible_at` is null is
invisible to everyone except its author, including to admins on the read path —
they can see it in the database if a dispute needs it, which is a different
thing from the product showing it.

**Ratings are shown, never ranked.** A cleaner's average is display only.
Rating-weighted search ranking is explicitly out of scope for v1 (CLAUDE.md),
and the bench board's ordering stays urgency-first — a new cleaner with no
reviews must not be sorted to the bottom of a marketplace that needs supply.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.award import Award
from app.models.enums import TurnoverStatus, UserRole
from app.models.property import Property
from app.models.review import Review
from app.models.turnover import Turnover
from app.models.user import User
from app.services import notifications


class ReviewRefused(Exception):
    """This review cannot be written, and the reason is sayable."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def reveal_window() -> timedelta:
    return timedelta(days=settings.review_reveal_after_days)


@dataclass(frozen=True)
class Participants:
    """The two people a turnover's reviews are between."""

    owner: User
    cleaner: User
    award: Award


def participants(db: Session, turnover: Turnover) -> Participants | None:
    """Who may review this turnover, or None if nobody may.

    Reviews are for **work that happened**. A job is reviewable once the cleaner
    marked it complete — the same transition money hangs off, and for the same
    reason: a turnover whose checkout time has passed is not evidence anybody
    cleaned anything.

    A cancellation or a no-show is deliberately **not** reviewable. There is no
    mutual review to be had when one side did not turn up, so the delayed reveal
    has nothing to balance — and the product already records it where it belongs:
    `was_no_show` on the award, an alert to an admin, and a dispute answered by a
    human rather than by a star rating (CLAUDE.md).
    """
    award = db.execute(
        select(Award).where(
            Award.turnover_id == turnover.id, Award.cancelled_at.is_(None)
        )
    ).scalar_one_or_none()
    if award is None or award.completed_at is None:
        return None
    if turnover.status is not TurnoverStatus.COMPLETED:
        return None

    prop = db.get(Property, turnover.property_id)
    if prop is None:
        return None
    owner = db.get(User, prop.owner_id)
    cleaner = db.get(User, award.cleaner_id)
    if owner is None or cleaner is None:
        return None

    return Participants(owner=owner, cleaner=cleaner, award=award)


def role_for(people: Participants, user: User) -> UserRole | None:
    """Which side of this turnover the user is on, if either."""
    if user.id == people.owner.id:
        return UserRole.OWNER
    if user.id == people.cleaner.id:
        return UserRole.CLEANER
    return None


def all_on(db: Session, turnover_id: uuid.UUID) -> list[Review]:
    """Every review on a turnover, visible or not. **Service-internal.**

    Callers outside this module use it only to ask `too_late`; nothing builds a
    response from it, because it contains the hidden half.
    """
    return _existing(db, turnover_id)


def _existing(db: Session, turnover_id: uuid.UUID) -> list[Review]:
    return list(
        db.execute(select(Review).where(Review.turnover_id == turnover_id))
        .scalars()
        .all()
    )


def counterpart_of(people: Participants, role: UserRole) -> User:
    return people.cleaner if role is UserRole.OWNER else people.owner


# --------------------------------------------------------------------------
# Writing one
# --------------------------------------------------------------------------


def too_late(existing: list[Review], role: UserRole) -> bool:
    """Whether this side missed their window because the other's is already out.

    True only once the counterpart's review is *visible* — which, given both are
    revealed together, can only have happened through the timeout sweep. Writing
    after that would mean writing with sight of theirs.
    """
    return any(
        review.author_role is not role and review.visible_at is not None
        for review in existing
    )


def submit(
    db: Session,
    *,
    turnover: Turnover,
    author: User,
    rating: int,
    text: str | None,
) -> Review:
    """Write this side's review. **The turnover row must already be locked.**

    Locked for the same reason accepting a bid is: two sides submitting at once
    both need to see whether the other has, and a check made against a stale
    read is how both reviews end up written with neither revealed — each one
    having looked before the other's row existed.

    Does not reveal on its own. If this is the second review, `reveal_pair`
    below opens both in the same transaction.
    """
    people = participants(db, turnover)
    if people is None:
        raise ReviewRefused(
            "This turnover is not finished, so there is nothing to review yet."
        )

    role = role_for(people, author)
    if role is None:
        raise ReviewRefused("You were not part of this turnover.")

    existing = _existing(db, turnover.id)
    if any(review.author_role is role for review in existing):
        # Deliberately not an update. A review you can rewrite after the other
        # side's appears is a review you can rewrite *in response to* it, which
        # is the behaviour the whole delay exists to prevent — and the unique
        # constraint on (turnover_id, author_role) is the backstop.
        raise ReviewRefused("You have already reviewed this turnover.")

    if too_late(existing, role):
        # **The window closing is what makes waiting a bad strategy.**
        #
        # Without this, stalling beats reviewing honestly: say nothing for
        # fourteen days, let the sweep reveal theirs, read it, and then write
        # yours knowing exactly what it has to answer — and they cannot reply,
        # because there are no edits and one review per side. That is the same
        # informed, unanswerable review the no-edits rule refuses, reached by
        # writing rather than by rewriting.
        #
        # So the invariant is the stronger one it always meant to be: **no
        # review is ever written by somebody who has seen the other side's.**
        raise ReviewRefused(
            "Their review has already been published, so the window for yours "
            "has closed. Reviews are written without sight of each other — "
            "that is what makes them worth reading."
        )

    review = Review(
        turnover_id=turnover.id,
        author_id=author.id,
        author_role=role,
        rating=rating,
        text=(text or "").strip() or None,
    )
    db.add(review)
    db.flush()

    others = [r for r in existing if r.author_role is not role]
    if others:
        # Both sides are in. Open them together, now.
        reveal_pair(db, turnover, [review, *others])

    db.commit()
    notifications.deliver_pending(db)
    return review


# --------------------------------------------------------------------------
# Revealing — the one author of `visible_at`
# --------------------------------------------------------------------------


def reveal_pair(
    db: Session, turnover: Turnover, reviews: list[Review], *, now: datetime | None = None
) -> list[Review]:
    """Make these reviews visible, and tell both people. **Does not commit.**

    The only function in the codebase that writes `visible_at`. Everything that
    reveals — the second submission, the timeout sweep — comes through here, so
    "a review became visible" and "the people involved were told" cannot come
    apart. Queued inside the caller's transaction, delivered after it, the same
    contract as every other notification.
    """
    moment = now or datetime.now(timezone.utc)
    newly: list[Review] = []
    for review in reviews:
        if review.visible_at is None:
            review.visible_at = moment
            newly.append(review)

    if not newly:
        return []

    prop = db.get(Property, turnover.property_id)
    if prop is None:
        return newly

    for review in newly:
        # The person told is the one being reviewed, not the one who wrote it.
        subject_id = _subject_of(db, turnover, review)
        subject = db.get(User, subject_id) if subject_id else None
        if subject is not None:
            notifications.review_received(db, turnover, prop, review, subject)

    return newly


def _subject_of(db: Session, turnover: Turnover, review: Review) -> uuid.UUID | None:
    """Who this review is *about* — the other side from its author."""
    people = participants(db, turnover)
    if people is None:
        return None
    counterpart = counterpart_of(people, review.author_role)
    return counterpart.id


def due_for_reveal(db: Session, *, now: datetime | None = None) -> list[Review]:
    """One-sided reviews whose window has run out.

    Silence is not a veto. A review nobody answered is revealed once the window
    passes, because the alternative — invisible until the other side writes —
    makes refusing to write the way to bury a bad review.
    """
    reference = now or datetime.now(timezone.utc)
    cutoff = reference - reveal_window()

    return list(
        db.execute(
            select(Review)
            .where(Review.visible_at.is_(None), Review.created_at <= cutoff)
            .order_by(Review.created_at)
        )
        .scalars()
        .all()
    )


def reveal_overdue(db: Session, *, now: datetime | None = None) -> int:
    """Reveal everything past its window. Called from the scheduled pass.

    Commits, but deliberately does **not** drain the outbox. The scheduled pass
    drains once at the end, after everything it queues; draining here as well
    would send the same rows and then report them as undelivered, because the
    second drain finds nothing left. The rule across the codebase is the same
    either way — a *request* clears what it queued, a scheduled run clears
    everything once.
    """
    revealed = 0
    for review in due_for_reveal(db, now=now):
        turnover = db.get(Turnover, review.turnover_id)
        if turnover is None:
            continue
        revealed += len(reveal_pair(db, turnover, [review], now=now))

    db.commit()
    return revealed


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def visible_for(db: Session, turnover_id: uuid.UUID) -> list[Review]:
    """The reviews on this turnover that everyone can see."""
    return list(
        db.execute(
            select(Review)
            .where(Review.turnover_id == turnover_id, Review.visible_at.is_not(None))
            .order_by(Review.created_at)
        )
        .scalars()
        .all()
    )


def open_review_windows(db: Session, user: User) -> set[uuid.UUID]:
    """Turnovers this person may still review. **The same rule `submit` applies.**

    Exists because the window now closes for good: once the sweep publishes the
    other side's review, `too_late` refuses yours permanently — there are no
    edits and one review per side. A window that can be missed by never finding
    a screen is a window that gets missed, so the owner's turnover list asks
    this rather than hiding a finished job behind a toggle.

    **It gives nothing away.** It is true while the job is finished, you have
    not written, and the other side's review is not yet *visible* — and a
    visible review is one you can already read. So it says nothing about whether
    they have written, which is the one fact the delay withholds.

    Both sides in one query, because reviewing is the symmetrical part of the
    product: an owner reads it through their turnovers, a cleaner through their
    jobs.
    """
    mine_exists = (
        select(Review.id)
        .where(Review.turnover_id == Turnover.id, Review.author_id == user.id)
        .exists()
    )
    theirs_is_out = (
        select(Review.id)
        .where(
            Review.turnover_id == Turnover.id,
            Review.author_id != user.id,
            Review.visible_at.is_not(None),
        )
        .exists()
    )

    rows = db.execute(
        select(Turnover.id)
        .join(Property, Property.id == Turnover.property_id)
        .join(Award, Award.turnover_id == Turnover.id)
        .where(
            Turnover.status == TurnoverStatus.COMPLETED,
            Award.cancelled_at.is_(None),
            Award.completed_at.is_not(None),
            # Either side of the job, the same rights.
            (Property.owner_id == user.id) | (Award.cleaner_id == user.id),
            ~mine_exists,
            ~theirs_is_out,
        )
    ).scalars()
    return set(rows)


def own_review(db: Session, turnover_id: uuid.UUID, author: User) -> Review | None:
    """The review this person wrote, visible or not.

    Your own is always readable — you wrote it, so showing it back tells you
    nothing you did not already know. The other side's is a different question
    and goes through `visible_for`.
    """
    return db.execute(
        select(Review).where(
            Review.turnover_id == turnover_id, Review.author_id == author.id
        )
    ).scalar_one_or_none()


@dataclass(frozen=True)
class Reputation:
    """What a person's visible reviews add up to.

    **Display only.** Rating-weighted search ranking is out of scope for v1, and
    the board still sorts by urgency — a cleaner with no reviews yet must not be
    pushed to the bottom of a marketplace that is short of supply.
    """

    count: int
    #: Average of visible ratings, rounded to one decimal, or None with no
    #: reviews. Rounded here rather than in the frontend so every screen shows
    #: the same number.
    average: float | None


def reputation_of(db: Session, user_id: uuid.UUID) -> Reputation:
    """Someone's rating from the reviews written *about* them.

    Keyed on the counterpart rather than the author: a cleaner's reputation is
    what owners said about them, which is the opposite side of each row from the
    author. Invisible reviews are excluded — a rating built from reviews nobody
    can read would leak the hidden half.
    """
    # Reviews about this user as the cleaner: written by the owner on a
    # turnover this user was awarded.
    as_cleaner = (
        select(Review.rating)
        .join(Award, Award.turnover_id == Review.turnover_id)
        .where(
            Review.author_role == UserRole.OWNER,
            Review.visible_at.is_not(None),
            Award.cleaner_id == user_id,
            Award.cancelled_at.is_(None),
        )
    )
    # Reviews about this user as the owner: written by the cleaner on a
    # turnover at a property this user owns.
    as_owner = (
        select(Review.rating)
        .join(Turnover, Turnover.id == Review.turnover_id)
        .join(Property, Property.id == Turnover.property_id)
        .where(
            Review.author_role == UserRole.CLEANER,
            Review.visible_at.is_not(None),
            Property.owner_id == user_id,
        )
    )

    ratings = [
        *db.execute(as_cleaner).scalars().all(),
        *db.execute(as_owner).scalars().all(),
    ]
    if not ratings:
        return Reputation(count=0, average=None)

    # Integer sum, one division, rounded once. Nothing accumulates a float.
    return Reputation(count=len(ratings), average=round(sum(ratings) / len(ratings), 1))


def reputation_counts(db: Session, cleaner_ids: list[uuid.UUID]) -> dict[uuid.UUID, Reputation]:
    """Reputations for several cleaners at once, for a list screen.

    The same definition as `reputation_of` — visible reviews only, written by
    the owner about the cleaner on an uncancelled award — in one query rather
    than N, because a job with twenty bids on it must not become twenty-one
    round trips.

    `reputation_of` also counts the reviews written *about somebody as an
    owner*; this one does not, because every id it is given is a cleaner and
    that half would be empty. A test asserts the two agree for cleaners, so the
    optimisation cannot quietly become a second definition.
    """
    if not cleaner_ids:
        return {}

    rows = db.execute(
        select(
            Award.cleaner_id,
            func.count(Review.rating),
            func.avg(Review.rating),
        )
        .join(Review, Review.turnover_id == Award.turnover_id)
        .where(
            Award.cleaner_id.in_(cleaner_ids),
            Award.cancelled_at.is_(None),
            Review.author_role == UserRole.OWNER,
            Review.visible_at.is_not(None),
        )
        .group_by(Award.cleaner_id)
    ).all()

    found = {
        cleaner_id: Reputation(count=count, average=round(float(average), 1))
        for cleaner_id, count, average in rows
    }
    return {
        cleaner_id: found.get(cleaner_id, Reputation(count=0, average=None))
        for cleaner_id in cleaner_ids
    }
