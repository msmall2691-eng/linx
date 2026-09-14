"""Notifications — who hears about what, and the proof that they did.

This module replaces phase 4's `alerts.py`. That one named an event and logged
it; this one records a row, resolves recipients from roles rather than
addresses, and delivers through `app.services.delivery`.

Three rules from CLAUDE.md shape the whole thing:

**One place decides recipients.** Call sites say what happened — a bid was
placed, an award came undone. They do not say who to tell. That is decided here,
from roles: "admin" is a role, not an address, and an address hardcoded at a
call site stops alerting the day somebody new takes over the inbox.

**Record inside the transaction, deliver after it.** `queue()` adds rows to the
caller's open transaction, so a notification cannot describe a state change that
then rolled back. `deliver_pending()` runs after the commit and is a plain
outbox drain: a process that dies between the two leaves rows that the next run
picks up, rather than a send nobody can account for.

**Duplicates are as bad as misses.** Every row carries a `dedupe_key` derived
from the event and its subject — a turnover id, a bid id, a recipient — never
from a clock or a random value. The unique constraint makes "fires once per
transition" a property of the database. It is what lets the reminder job run
every fifteen minutes without sending fifteen-minute reminders.

The three events at the bottom of the fixed list — payment receipt, payout
notice, review received — have no state transition to fire them yet. They are
declared in `NotificationEvent` and wired in phases 6 and 7. Nothing here
pretends otherwise.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import Float, cast, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.award import Award
from app.models.bid import Bid
from app.models.cleaner_profile import CleanerProfile
from app.models.enums import (
    NotificationEvent,
    NotificationStatus,
    TurnoverStatus,
    UserRole,
)
from app.models.notification import Notification
from app.models.property import Property
from app.models.turnover import Turnover
from app.models.user import User
from app.services import delivery
from app.services.geo import distance_miles_sql
from app.services.urgency import region_timezone

logger = logging.getLogger("linx.notifications")

#: How many pending rows one drain handles. Large enough that a normal request
#: clears its own notifications, small enough that a backlog cannot turn one
#: request into a mail run.
DRAIN_LIMIT = 100


# --------------------------------------------------------------------------
# Recipients — resolved from roles, never from addresses at a call site
# --------------------------------------------------------------------------


def owner_of(db: Session, turnover: Turnover) -> User | None:
    return db.execute(
        select(User)
        .join(Property, Property.owner_id == User.id)
        .where(Property.id == turnover.property_id)
    ).scalar_one_or_none()


def admins(db: Session) -> list[User]:
    """Every active admin. The human inbox disputes and no-shows land in."""
    return list(
        db.execute(
            select(User)
            .where(User.role == UserRole.ADMIN, User.is_active.is_(True))
            .order_by(User.email)
        )
        .scalars()
        .all()
    )


def cleaners_in_range(db: Session, prop: Property) -> list[User]:
    """Cleared cleaners whose service area covers this property.

    Cleared ones only, deliberately. A cleaner who has not finished vetting
    cannot bid, and a message about work they are not allowed to take is the
    kind of noise that teaches people to ignore the sender. They can still see
    the job on the board; what they get told about is work they can act on.
    """
    if prop.lat is None or prop.lng is None:
        return []

    distance = distance_miles_sql(
        CleanerProfile.service_lat, CleanerProfile.service_lng, prop.lat, prop.lng
    )
    return list(
        db.execute(
            select(User)
            .join(CleanerProfile, CleanerProfile.user_id == User.id)
            .where(
                User.is_active.is_(True),
                CleanerProfile.can_take_jobs.is_(True),
                CleanerProfile.service_lat.is_not(None),
                CleanerProfile.service_lng.is_not(None),
                distance <= cast(CleanerProfile.service_radius_miles, Float),
            )
            .order_by(User.email)
        )
        .scalars()
        .all()
    )


# --------------------------------------------------------------------------
# Queueing and delivery
# --------------------------------------------------------------------------


def queue(
    db: Session,
    event: NotificationEvent,
    *,
    recipients: list[User],
    subject: str,
    body: str,
    dedupe_scope: str,
    turnover_id: uuid.UUID | None = None,
) -> list[Notification]:
    """Record that these people are owed this message. **Does not commit.**

    Called inside the caller's open transaction, so the notification and the
    state change it describes land together or not at all.

    `dedupe_scope` identifies *what this is about* — a turnover id, a bid id and
    its price, an award id. Combined with the event and the recipient it becomes
    the unique key. Re-queueing the same thing is a no-op rather than an error:
    each insert gets its own savepoint, so one already-queued recipient cannot
    roll back the others or the caller's own work.
    """
    queued: list[Notification] = []
    for user in recipients:
        if not user.email:
            # Nothing to send to. Loud, because a recipient with no address is
            # a person who will not be told and will not know they were not.
            logger.warning("no address for recipient %s on %s", user.id, event.value)
            continue

        row = Notification(
            event=event,
            recipient_id=user.id,
            destination=user.email,
            turnover_id=turnover_id,
            dedupe_key=f"{event.value}:{dedupe_scope}:{user.id}",
            subject=subject,
            body=body,
        )
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
        except IntegrityError:
            # Already queued. That is the dedupe working, not a failure.
            continue
        queued.append(row)

    return queued


def deliver_pending(db: Session, *, limit: int = DRAIN_LIMIT) -> int:
    """Drain the outbox. Call **after** the commit that queued the rows.

    Returns how many were delivered. Each row is marked attempted and committed
    *before* its send, so a process that dies mid-send leaves a row that is
    visibly attempted rather than one that looks untouched — the same rule
    guardrail 2 applies to a Stripe call, for the same reason: an unknown
    outcome must never read as a success.
    """
    pending = list(
        db.execute(
            select(Notification)
            .where(Notification.status == NotificationStatus.PENDING)
            .order_by(Notification.created_at)
            .limit(limit)
        )
        .scalars()
        .all()
    )
    if not pending:
        return 0

    sender = delivery.get_sender()
    delivered = 0

    for row in pending:
        row.attempted_at = datetime.now(timezone.utc)
        db.commit()

        try:
            sender.send(
                delivery.Outgoing(
                    destination=row.destination, subject=row.subject, body=row.body
                )
            )
        except delivery.DeliveryError as exc:
            row.status = NotificationStatus.FAILED
            row.failure_message = str(exc)
            db.commit()
            logger.warning("delivery failed for %s: %s", row.id, exc)
            continue

        if sender.delivers:
            row.status = NotificationStatus.SENT
            row.sent_at = datetime.now(timezone.utc)
            delivered += 1
        else:
            # The logging sender. The row stays pending with an attempt on it,
            # because nothing was actually delivered and saying otherwise would
            # make the record lie.
            row.failure_message = "no SMTP host configured; logged only"
        db.commit()

    return delivered


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------


def _when(moment: datetime) -> str:
    """A timestamp as the person reading it experiences it — region-local.

    Never UTC in a message. A cleaner reading "checkout 3pm" needs that to be
    3pm where the house is.
    """
    return moment.astimezone(region_timezone()).strftime("%a %-d %b, %-I:%M %p")


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _where(prop: Property) -> str:
    return f"{prop.nickname} — {prop.city}, {prop.state}"


# --------------------------------------------------------------------------
# The events
# --------------------------------------------------------------------------


def turnover_posted(db: Session, turnover: Turnover, prop: Property) -> list[Notification]:
    """A new job landed inside somebody's service radius."""
    recipients = cleaners_in_range(db, prop)
    return queue(
        db,
        NotificationEvent.TURNOVER_POSTED,
        recipients=recipients,
        subject=f"New turnover near you: {_where(prop)}",
        body=(
            f"A turnover was just posted in your service area.\n\n"
            f"Where: {_where(prop)}\n"
            f"Checkout: {_when(turnover.checkout_at)}\n"
            + (
                f"Next checkin: {_when(turnover.checkin_at)}\n"
                if turnover.checkin_at
                else "Next checkin: none booked yet\n"
            )
            + (
                f"Owner's budget: {_money(turnover.owner_budget_cents)}\n"
                if turnover.owner_budget_cents
                else ""
            )
            + "\nOpen your board to see it and name a price."
        ),
        dedupe_scope=str(turnover.id),
        turnover_id=turnover.id,
    )


def bid_received(
    db: Session, bid: Bid, turnover: Turnover, prop: Property
) -> list[Notification]:
    """A cleaner named a price on the owner's job.

    The dedupe scope includes the price, so a cleaner *changing* their number
    tells the owner again — it is new information — while a resubmission at the
    same price does not.
    """
    owner = owner_of(db, turnover)
    if owner is None:
        return []
    return queue(
        db,
        NotificationEvent.BID_RECEIVED,
        recipients=[owner],
        subject=f"New bid on {prop.nickname}: {_money(bid.price_cents)}",
        body=(
            f"A cleaner bid {_money(bid.price_cents)} on your turnover.\n\n"
            f"Where: {_where(prop)}\n"
            f"Checkout: {_when(turnover.checkout_at)}\n"
            + (f"\nThey said: {bid.message}\n" if bid.message else "")
            + "\nOpen the turnover to accept or decline."
        ),
        dedupe_scope=f"{bid.id}:{bid.price_cents}",
        turnover_id=turnover.id,
    )


def bid_accepted(
    db: Session, award: Award, turnover: Turnover, prop: Property
) -> list[Notification]:
    """The cleaner got the job — and with it, the address and the access notes."""
    cleaner = db.get(User, award.cleaner_id)
    if cleaner is None:
        return []
    return queue(
        db,
        NotificationEvent.BID_ACCEPTED,
        recipients=[cleaner],
        subject=f"You got the job: {_where(prop)}",
        body=(
            f"Your bid of {_money(award.agreed_price_cents)} was accepted.\n\n"
            f"Where: {_where(prop)}\n"
            f"Checkout: {_when(turnover.checkout_at)}\n"
            + (
                f"Next checkin: {_when(turnover.checkin_at)}\n"
                if turnover.checkin_at
                else ""
            )
            + "\nThe full address and how to get in are on the job in your "
            "account. If you can't make it, cancel there as early as you can — "
            "the owner and an admin are told either way."
        ),
        dedupe_scope=str(award.id),
        turnover_id=turnover.id,
    )


def bids_declined(
    db: Session, turnover: Turnover, prop: Property, bids: list[Bid]
) -> list[Notification]:
    """Everyone who did not get it hears so, rather than being left pending.

    Two different things end a bid without winning it: the owner hired somebody
    else, or the owner said no to this one and hired nobody. They are the same
    event and the same recipient, but not the same sentence — telling a cleaner
    the job went to someone else when it is still open sends them away from a
    job they could still bid on.
    """
    someone_was_hired = turnover.status is TurnoverStatus.AWARDED
    reason = (
        "The owner went with another cleaner for this turnover."
        if someone_was_hired
        else "The owner passed on your bid for this turnover."
    )
    closing = (
        "Your board has the jobs still open near you."
        if someone_was_hired
        else "It is still open on the board if you want to bid again."
    )

    queued: list[Notification] = []
    for bid in bids:
        cleaner = db.get(User, bid.cleaner_id)
        if cleaner is None:
            continue
        queued.extend(
            queue(
                db,
                NotificationEvent.BID_DECLINED,
                recipients=[cleaner],
                subject=f"Not this time: {_where(prop)}",
                body=(
                    f"{reason}\n\n"
                    f"Where: {_where(prop)}\n"
                    f"Checkout: {_when(turnover.checkout_at)}\n\n"
                    f"{closing}"
                ),
                dedupe_scope=str(bid.id),
                turnover_id=turnover.id,
            )
        )
    return queued


def award_cancelled(
    db: Session,
    *,
    turnover: Turnover,
    prop: Property,
    award: Award,
    actor: User,
    no_show: bool,
    late: bool,
) -> list[Notification]:
    """A booking came undone. Both sides and an admin, every time.

    Never conditional on the cancellation being late — "late" changes how the
    message reads, not whether it is sent.
    """
    if no_show:
        event = NotificationEvent.CLEANER_NO_SHOW
        headline = "The cleaner did not turn up."
    elif actor.id == award.cleaner_id:
        event = NotificationEvent.CLEANER_CANCELLED
        headline = (
            "The cleaner cancelled less than 48 hours before checkout."
            if late
            else "The cleaner cancelled."
        )
    else:
        event = NotificationEvent.OWNER_CANCELLED_AWARDED
        headline = "The owner cancelled this turnover."

    owner = owner_of(db, turnover)
    cleaner = db.get(User, award.cleaner_id)
    recipients = [user for user in (owner, cleaner, *admins(db)) if user is not None]

    reopened = event is not NotificationEvent.OWNER_CANCELLED_AWARDED
    return queue(
        db,
        event,
        recipients=recipients,
        subject=f"Booking cancelled: {_where(prop)}",
        body=(
            f"{headline}\n\n"
            f"Where: {_where(prop)}\n"
            f"Checkout: {_when(turnover.checkout_at)}\n"
            + (f"Reason given: {award.cancellation_reason}\n" if award.cancellation_reason else "")
            + "\n"
            + (
                "The turnover is back on the bench and open for bids.\n"
                if reopened
                else "The turnover is cancelled; nobody is booked for it.\n"
            )
        ),
        dedupe_scope=str(award.id),
        turnover_id=turnover.id,
    )


def turnover_reminder(
    db: Session, turnover: Turnover, prop: Property, award: Award
) -> list[Notification]:
    """Day-of reminder, both sides, once."""
    owner = owner_of(db, turnover)
    cleaner = db.get(User, award.cleaner_id)
    recipients = [user for user in (owner, cleaner) if user is not None]
    return queue(
        db,
        NotificationEvent.TURNOVER_REMINDER,
        recipients=recipients,
        subject=f"Tomorrow: {_where(prop)}",
        body=(
            f"A reminder that this turnover is coming up.\n\n"
            f"Where: {_where(prop)}\n"
            f"Checkout: {_when(turnover.checkout_at)}\n"
            + (
                f"Next checkin: {_when(turnover.checkin_at)}\n"
                if turnover.checkin_at
                else ""
            )
            + f"Agreed price: {_money(award.agreed_price_cents)}\n"
        ),
        dedupe_scope=str(turnover.id),
        turnover_id=turnover.id,
    )


def turnover_unclaimed(
    db: Session, turnover: Turnover, prop: Property
) -> list[Notification]:
    """Nobody has taken this and checkout is close. Owner and admin.

    Deliberately its own alarm rather than a rung on the urgency ladder: it has
    its own cutoff and its own recipients, and it is about *staffing* rather
    than about the schedule.
    """
    owner = owner_of(db, turnover)
    recipients = [user for user in (owner, *admins(db)) if user is not None]
    return queue(
        db,
        NotificationEvent.TURNOVER_UNCLAIMED,
        recipients=recipients,
        subject=f"Still unclaimed: {_where(prop)}",
        body=(
            "Nobody has been booked for this turnover and checkout is close.\n\n"
            f"Where: {_where(prop)}\n"
            f"Checkout: {_when(turnover.checkout_at)}\n"
            + (
                f"Owner's budget: {_money(turnover.owner_budget_cents)}\n"
                if turnover.owner_budget_cents
                else "No budget posted.\n"
            )
            + "\nRaising the budget is usually what moves it."
        ),
        dedupe_scope=str(turnover.id),
        turnover_id=turnover.id,
    )
