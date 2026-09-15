"""When a background task last finished, and how it went.

**One row per task name, overwritten each pass.** Not a log — a log answers
"what happened in April" and this answers the only question that matters
operationally: *is the thing running at all right now.*

It exists because the scheduled pass is the one part of this product that
nothing tells you about when it stops. A web request that fails throws an
error somebody sees. A cron service that was never created, or that has been
failing since the last deploy, looks exactly like a quiet week — and three
things hang off it, one of which is load-bearing rather than a courtesy:

* the day-of reminder stops, and two people find out by turning up;
* the unclaimed alarm stops, and nobody hears that tomorrow's job has no
  cleaner;
* **one-sided reviews are never revealed**, which turns refusing to answer into
  the way to bury a bad review — the exact suppression the delay exists to
  prevent, achieved by doing nothing (CLAUDE.md).

Inferring it from other rows does not work, and that is worth writing down
because it is the obvious first idea. The newest notification, the newest
calendar sync — each is silent on a genuinely quiet pass, so "no evidence" and
"not running" look the same. This is a fact the pass records about itself.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TaskRun(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """The last completed run of one named background task."""

    __tablename__ = "task_runs"

    #: The task's name, e.g. "scheduled". Unique: this table holds the latest
    #: run, not a history, so a second row for the same task would mean two
    #: answers to a one-answer question.
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    #: When the pass finished. **Finished, not started** — a pass that began and
    #: died is not evidence that the work was done, and recording the start
    #: would make a task that crashes every time look perfectly healthy.
    finished_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    #: How long it took, for a person deciding whether the schedule is tight.
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: What it did, as a short human-readable line. Free text on purpose: the
    #: pass returns a different set of counters as the product grows, and a
    #: column per counter is a migration every time.
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<TaskRun {self.name} at {self.finished_at.isoformat()}>"
