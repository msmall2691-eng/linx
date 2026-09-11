"""Turnover helpers — keeping the derived fields honest.

`urgency` and `is_same_day` are stored columns, because the bench board sorts
and filters on them and a board cannot sort by a value computed in Python after
the query. Stored means they can go stale, so this module owns the one function
that writes them.

Staleness only affects standing vacancies. A turnover with a known checkin has
an urgency fixed by two immutable-until-edited timestamps; a vacancy's urgency
is measured against *now*, so it climbs the ladder as checkout approaches. That
is why the read paths call `refresh_urgency` and persist a change when they find
one: the alternative is a column that silently disagrees with what the same rule
would say today, which is the exact failure the single-source-of-truth rule
exists to prevent.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models.turnover import Turnover
from app.services.urgency import derive_urgency, is_same_day


def apply_derived_fields(turnover: Turnover, *, now: datetime | None = None) -> bool:
    """Set `is_same_day` and `urgency` from the schedule.

    Returns True if either value changed, so callers can skip a pointless write.
    Call this after anything that touches `checkout_at` or `checkin_at`.
    """
    same_day = is_same_day(turnover.checkout_at, turnover.checkin_at)
    urgency = derive_urgency(turnover.checkout_at, turnover.checkin_at, now=now)

    changed = turnover.is_same_day != same_day or turnover.urgency != urgency
    turnover.is_same_day = same_day
    turnover.urgency = urgency
    return changed


def refresh_urgency(
    db: Session,
    turnovers: list[Turnover],
    *,
    now: datetime | None = None,
) -> None:
    """Bring a batch of turnovers' derived fields up to date, committing once.

    Read paths call this so a standing vacancy that has climbed the ladder since
    it was last written is displayed — and sorted — at its real urgency.
    """
    if any([apply_derived_fields(t, now=now) for t in turnovers]):
        db.commit()
