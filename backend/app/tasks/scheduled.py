"""The work nothing triggers: the day-of reminder, the alarm, and the reveal.

Most of the product hangs off something a person did — a bid was placed, an
award was accepted, a booking came undone. These three hang off the clock
passing a line, so they need something that runs.

    python -m app.tasks.scheduled

Run it on a schedule (on Railway, a cron service on the same image). **Running
it often is safe and intended.** Both events key their `dedupe_key` off the
turnover id, so the second pass through the same window writes nothing — the
unique constraint is what makes a five-minute cron produce one reminder rather
than twelve. That is the whole reason the key exists.

The job also drains the outbox, so a notification queued by a request whose
process died before delivery still goes out on the next run rather than sitting
as a row nobody ever reads.

The windows are deliberately separate numbers (`REMINDER_HOURS_BEFORE`,
`UNCLAIMED_ALERT_HOURS_BEFORE`, `REVIEW_REVEAL_AFTER_DAYS`). One is a courtesy
to two people who already have a booking, one is an operational alarm about work
nobody has taken, and one is how long a review waits for an answer that may
never come. Tying any two together would mean tuning one changes another.

The reveal is the one here that is **load-bearing rather than a courtesy**: a
one-sided review that is never revealed makes silence a veto, and refusing to
answer becomes the way to bury a bad review. If this job stops running, that is
what quietly stops working.
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
from app.services import awards, calendars, geocoding, notifications, reviews

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
            # Both live-booking statuses: a cleaner who marked themselves on
            # site early must not silently switch off the owner's reminder.
            Turnover.status.in_(awards.LIVE_BOOKING_STATUSES),
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


def reveal_reviews(db: Session, *, now: datetime | None = None) -> int:
    """Open one-sided reviews whose window has run out.

    Silence must not be a veto. `app/services/reviews.py` owns the rule and the
    only write to `visible_at`; this just gives it a clock.
    """
    return reviews.reveal_overdue(db, now=now)


def place_unmapped_properties(db: Session) -> int:
    """Give coordinates to any property that has none. **Repairs a silent hole.**

    The bench board's radius query requires `Property.lat IS NOT NULL`, and
    until phase 8 nothing in the application ever set those columns — only the
    browser tests did, straight into the database. Every property created
    through the real site was therefore invisible to every cleaner, on screens
    that looked entirely normal to its owner.

    New properties are placed on save. This is for the ones already saved:
    it runs on every scheduled pass, costs one indexed query when there is
    nothing to do, and needs nobody to remember to run a script.
    """
    rows = db.execute(
        select(Property).where(
            (Property.lat.is_(None)) | (Property.lng.is_(None))
        )
    ).scalars().all()

    for prop in rows:
        fix = geocoding.locate(postal_code=prop.postal_code, city=prop.city)
        prop.lat, prop.lng = fix.lat, fix.lng
        logger.info(
            "placed property %s from its %s: %.4f, %.4f",
            prop.id,
            fix.source,
            fix.lat,
            fix.lng,
        )

    if rows:
        db.commit()
    return len(rows)


def sync_calendars(db: Session) -> int:
    """Read every connected booking feed. Returns how many drafts it created.

    **One feed failing must not stop the rest.** Each gets its own try: a
    listing site having a bad afternoon, or one owner's expired link, is exactly
    the situation where every other calendar most needs to keep working. The
    reason is recorded on the failing row where its owner can see it, and this
    pass moves on.
    """
    created = 0
    for calendar in calendars.active_calendars(db):
        try:
            created += calendars.sync(db, calendar).created
        except calendars.CalendarError as error:
            logger.info("calendar %s could not be read: %s", calendar.id, error.detail)
        except Exception:  # noqa: BLE001 - one bad feed must not end the pass
            logger.exception("calendar %s failed unexpectedly", calendar.id)
            db.rollback()
    return created


def run(db: Session, *, now: datetime | None = None) -> dict[str, int]:
    """One pass. Queue what is due, then send everything owed."""
    placed = place_unmapped_properties(db)
    synced = sync_calendars(db)
    reminders = send_reminders(db, now=now)
    unclaimed = alert_unclaimed(db, now=now)
    revealed = reveal_reviews(db, now=now)
    delivered = notifications.deliver_pending(db, limit=SCHEDULED_DRAIN_LIMIT)
    return {
        "placed": placed,
        "synced": synced,
        "reminders": reminders,
        "unclaimed": unclaimed,
        "revealed": revealed,
        "delivered": delivered,
    }


def main() -> None:  # pragma: no cover - exercised through run()
    from app.db import SessionLocal

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    db = SessionLocal()
    try:
        result = run(db)
    finally:
        db.close()
    logger.info(
        "scheduled pass: %d properties placed, %d drafts from calendars, "
        "%d reminders queued, %d unclaimed alerts queued, %d reviews revealed, "
        "%d delivered",
        result["placed"],
        result["synced"],
        result["reminders"],
        result["unclaimed"],
        result["revealed"],
        result["delivered"],
    )


if __name__ == "__main__":  # pragma: no cover
    main()
