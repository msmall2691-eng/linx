"""Operational alerts — who hears about what, decided in one place.

**Delivery is phase 5.** This module is deliberately not a notification system:
it has no templates, no queue, no email or SMS client, and no `notifications`
table. What it does have is the thing phase 4 cannot ship without — a single
place that names the event, works out every recipient, and records that the
alert happened, so that "the owner and admin are alerted immediately, never
silently" is something the code does rather than something the docs promise.

Phase 5 replaces the *sink* (`_deliver`) with real email and SMS and fills in
the rest of the fixed event list from CLAUDE.md. It does not have to revisit the
call sites, because the call sites already say what happened and to whom.

Two rules worth keeping when that happens:

* **Recipients are resolved here, not at the call site.** "Admin" is a role, not
  an address, and a route that hardcodes one address is a route that stops
  alerting the day someone new takes over the inbox.
* **An alert that cannot be delivered is still recorded.** The log line is
  written before anything tries to send, for the same reason guardrail 2 writes
  the attempt before the Stripe call: an unknown outcome must be visible, not
  assumed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import UserRole
from app.models.user import User

logger = logging.getLogger("linx.alerts")


class AlertEvent:
    """The event names phase 4 emits, from the fixed list in CLAUDE.md.

    A string constant rather than a Postgres enum: nothing stores these yet, and
    a native enum type would be a migration for a value that has no column.
    """

    CLEANER_CANCELLED = "cleaner_cancelled_awarded_turnover"
    CLEANER_NO_SHOW = "cleaner_no_show"
    OWNER_CANCELLED_AWARDED = "owner_cancelled_awarded_turnover"


@dataclass(frozen=True)
class Alert:
    event: str
    recipients: tuple[str, ...]
    summary: str
    context: dict[str, Any] = field(default_factory=dict)


def admin_emails(db: Session) -> list[str]:
    """Every active admin. The human inbox disputes and no-shows land in."""
    return list(
        db.execute(
            select(User.email)
            .where(User.role == UserRole.ADMIN, User.is_active.is_(True))
            .order_by(User.email)
        )
        .scalars()
        .all()
    )


def emit(
    event: str,
    *,
    recipients: list[str] | tuple[str, ...],
    summary: str,
    **context: Any,
) -> Alert:
    """Record that something happened and who needs to know.

    Returns the alert so a caller can attach it to a response or a test can
    assert on it. Duplicate and empty recipients are dropped — an alert
    addressed to nobody is the silent failure this whole module exists to
    prevent, so it is logged as a warning rather than passing quietly.
    """
    unique: list[str] = []
    for address in recipients:
        if address and address not in unique:
            unique.append(address)

    alert = Alert(event=event, recipients=tuple(unique), summary=summary, context=context)
    _deliver(alert)
    return alert


def _deliver(alert: Alert) -> None:
    """The sink. Phase 5 replaces this body; nothing above it has to change."""
    if not alert.recipients:
        logger.warning(
            "alert %s has no recipients: %s", alert.event, alert.summary, extra={"alert": alert}
        )
        return
    logger.info(
        "alert %s -> %s: %s",
        alert.event,
        ", ".join(alert.recipients),
        alert.summary,
        extra={"alert": alert},
    )
