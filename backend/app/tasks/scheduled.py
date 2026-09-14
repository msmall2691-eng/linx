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
from time import monotonic

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


#: How long the whole calendar pass may take, however many feeds there are.
#: Sized so a pass that runs every fifteen minutes cannot overlap itself on
#: calendars alone.
CALENDAR_PASS_BUDGET = 300.0


def sync_calendars(db: Session) -> tuple[int, int]:
    """Read every connected booking feed. Returns (drafts created, stale kept).

    **One feed failing must not stop the rest.** Each gets its own try: a
    listing site having a bad afternoon, or one owner's expired link, is exactly
    the situation where every other calendar most needs to keep working. The
    reason is recorded on the failing row where its owner can see it, and this
    pass moves on.

    **`stale_but_kept` is carried out of here, not dropped.** It counts jobs
    whose booking has vanished but which somebody had already acted on — a
    guest cancelled and a cleaner may still be coming. This is the pass that
    normally finds it, because it is the one that runs without anybody
    watching; taking only `.created` was how the product promised the owner a
    warning and then discarded it. `sync` also writes the number onto the
    calendar row, which is where the owner's own screen reads it from.
    """
    created = 0
    stale = 0
    deadline = monotonic() + CALENDAR_PASS_BUDGET
    for calendar in calendars.active_calendars(db):
        if monotonic() > deadline:
            # **A budget for the pass, not just for each feed.** Otherwise the
            # worst case is the number of feeds times the per-feed deadline,
            # which grows without limit as the product does.
            #
            # Fair because `active_calendars` is ordered oldest-sync-first: the
            # feeds skipped here are the first ones read next time, so nobody's
            # calendar is starved by somebody else's slow one.
            logger.warning(
                "calendar pass hit its %.0fs budget; the rest are first in line "
                "next pass",
                CALENDAR_PASS_BUDGET,
            )
            break
        try:
            result = calendars.sync(db, calendar)
        except calendars.CalendarError as error:
            logger.info("calendar %s could not be read: %s", calendar.id, error.detail)
            continue
        except Exception:  # noqa: BLE001 - one bad feed must not end the pass
            logger.exception("calendar %s failed unexpectedly", calendar.id)
            db.rollback()
            continue

        created += result.created
        stale += result.stale_but_kept
        if result.stale_but_kept:
            logger.warning(
                "calendar %s: %d job(s) whose booking is gone were kept because "
                "somebody is already on them",
                calendar.id,
                result.stale_but_kept,
            )
    return created, stale


def run(db: Session, *, now: datetime | None = None) -> dict[str, int]:
    """One pass. Queue what is due, send everything owed, *then* read feeds.

    **The order is the point.** Reading calendars is the only step here that
    waits on somebody else's server, and it used to run second — so every feed's
    deadline was spent before the reminders, the unclaimed alarm, the review
    reveal or the outbox drain had started. A per-feed limit does not fix that:
    fifty slow feeds is fifty times the limit, and the review reveal is the one
    scheduled job that is load-bearing rather than a courtesy. Silence becoming
    a veto because a listing site was slow is not a trade worth making.

    So the work that owes somebody something goes first and cannot be delayed by
    the network at all. Calendars go last, under a budget of their own, and the
    worst case is a feed read on the next pass instead of this one.

    Nothing is left unsent by that: sync writes drafts, and a draft notifies
    nobody — which is rule 1 of the calendar module, and is what makes this
    reordering safe rather than merely convenient.
    """
    placed = place_unmapped_properties(db)
    reminders = send_reminders(db, now=now)
    unclaimed = alert_unclaimed(db, now=now)
    revealed = reveal_reviews(db, now=now)
    delivered = notifications.deliver_pending(db, limit=SCHEDULED_DRAIN_LIMIT)
    synced, stale_bookings = sync_calendars(db)
    return {
        "placed": placed,
        "synced": synced,
        "stale_bookings": stale_bookings,
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
        "%d job(s) whose booking vanished, %d reminders queued, %d unclaimed "
        "alerts queued, %d reviews revealed, %d delivered",
        result["placed"],
        result["synced"],
        result["stale_bookings"],
        result["reminders"],
        result["unclaimed"],
        result["revealed"],
        result["delivered"],
    )


if __name__ == "__main__":  # pragma: no cover
    main()
