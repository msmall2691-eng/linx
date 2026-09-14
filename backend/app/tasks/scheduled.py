"""The two notifications nothing triggers: the day-of reminder, and the alarm.

Every other event in the fixed list hangs off something a person did — a bid was
placed, an award was accepted, a booking came undone. These two hang off the
clock passing a line, so they need something that runs.

    python -m app.tasks.scheduled

Run it on a schedule (on Railway, a cron service on the same image). **Running
it often is safe and intended.** Both events key their `dedupe_key` off the
turnover id, so the second pass through the same window writes nothing — the
unique constraint is what makes a five-minute cron produce one reminder rather
than twelve. That is the whole reason the key exists.

The job also drains the outbox, so a notification queued by a request whose
process died before delivery still goes out on the next run rather than sitting
as a row nobody ever reads.

The two windows are deliberately separate numbers (`REMINDER_HOURS_BEFORE`,
`UNCLAIMED_ALERT_HOURS_BEFORE`). One is a courtesy to two people who already
have a booking; the other is an operational alarm about work nobody has taken.
Tying them together would mean tuning the alarm changes the courtesy.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.award import Award
from app.models.enums import TurnoverStatus
from app.models.property import Property
from app.models.turnover import Turnover
from app.services import notifications

logger = logging.getLogger("linx.scheduled")

#: How far past checkout an unclaimed turnover keeps alarming. Without a floor,
#: a first run against an old database would alert on every job the product
#: never had, and the one that matters would be buried in them.
UNCLAIMED_LOOKBACK = timedelta(days=1)

#: The outbox drain is allowed to be much larger here than in a request: this
#: process exists to send, where a request only clears what it made.
SCHEDULED_DRAIN_LIMIT = 500


def send_reminders(db: Session, *, now: datetime | None = None) -> int:
    """Day-of reminder to both sides of every booked turnover coming up."""
    reference = now or datetime.now(timezone.utc)
    window_end = reference + timedelta(hours=settings.reminder_hours_before)

    rows = db.execute(
        select(Turnover, Property, Award)
        .join(Property, Turnover.property_id == Property.id)
        .join(Award, Award.turnover_id == Turnover.id)
        .where(
            Turnover.status == TurnoverStatus.AWARDED,
            Award.cancelled_at.is_(None),
            Turnover.checkout_at <= window_end,
            Turnover.checkout_at >= reference,
        )
        .order_by(Turnover.checkout_at)
    ).all()

    queued = 0
    for turnover, prop, award in rows:
        queued += len(notifications.turnover_reminder(db, turnover, prop, award))
    db.commit()
    return queued


def alert_unclaimed(db: Session, *, now: datetime | None = None) -> int:
    """Owner and admin, when checkout is close and nobody has taken the job.

    Deliberately not a rung on the urgency ladder. Urgency is a property of the
    schedule and is what a cleaner sorts the board by; this is a question about
    *staffing* that only matters because a date is approaching, and it has its
    own cutoff and its own recipients.
    """
    reference = now or datetime.now(timezone.utc)
    window_end = reference + timedelta(hours=settings.unclaimed_alert_hours_before)

    rows = db.execute(
        select(Turnover, Property)
        .join(Property, Turnover.property_id == Property.id)
        .where(
            Turnover.status == TurnoverStatus.OPEN,
            Turnover.checkout_at <= window_end,
            Turnover.checkout_at >= reference - UNCLAIMED_LOOKBACK,
        )
        .order_by(Turnover.checkout_at)
    ).all()

    queued = 0
    for turnover, prop in rows:
        queued += len(notifications.turnover_unclaimed(db, turnover, prop))
    db.commit()
    return queued


def run(db: Session, *, now: datetime | None = None) -> dict[str, int]:
    """One pass. Queue what is due, then send everything owed."""
    reminders = send_reminders(db, now=now)
    unclaimed = alert_unclaimed(db, now=now)
    delivered = notifications.deliver_pending(db, limit=SCHEDULED_DRAIN_LIMIT)
    return {"reminders": reminders, "unclaimed": unclaimed, "delivered": delivered}


def main() -> None:  # pragma: no cover - exercised through run()
    from app.db import SessionLocal

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    db = SessionLocal()
    try:
        result = run(db)
    finally:
        db.close()
    logger.info(
        "scheduled pass: %d reminders queued, %d unclaimed alerts queued, %d delivered",
        result["reminders"],
        result["unclaimed"],
        result["delivered"],
    )


if __name__ == "__main__":  # pragma: no cover
    main()
