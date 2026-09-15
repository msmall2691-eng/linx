"""Ask whether this deployment is ready for a pilot, and say so in the log.

    python -m app.tasks.launch_check

Exits non-zero when anything is blocking, so it can gate a deploy step rather
than being a thing somebody remembers to look at. It deliberately does **not**
exit non-zero for an unverifiable item: those need a person, and a command that
can never succeed is a command somebody starts passing `|| true`.

The distinction is the point. Blocking means this code checked and the answer
was no. Unverifiable means this code cannot see, and the summary says how many
of those are outstanding rather than quietly counting them as fine.
"""

from __future__ import annotations

import sys

from app.services import launch
from app.services.launch import State

#: Kept to plain ASCII: this runs in deploy logs, and a box-drawing character
#: that renders as a question mark makes a readiness report look broken.
MARK = {
    State.READY: "[ ok ]",
    State.BLOCKED: "[STOP]",
    State.ATTENTION: "[note]",
    State.UNVERIFIABLE: "[ask ]",
}


def render(checks: list[launch.Check]) -> str:
    lines = ["", "linx launch check", "=" * 60, ""]
    for check in checks:
        lines.append(f"{MARK[check.state]} {check.title}")
        lines.append(f"        {check.detail}")
        if check.remedy:
            lines.append(f"        -> {check.remedy}")
        lines.append("")

    blocking = launch.blocking(checks)
    asks = [c for c in checks if c.state is State.UNVERIFIABLE]
    lines.append("=" * 60)
    if blocking:
        lines.append(f"NOT READY: {len(blocking)} blocking item(s).")
    elif asks:
        lines.append(
            f"Nothing blocking. {len(asks)} item(s) this process cannot check — "
            "a person has to answer them before a pilot starts."
        )
    else:
        lines.append("Ready.")
    lines.append("")
    return "\n".join(lines)


def main() -> None:  # pragma: no cover - exercised through render()/run_checks()
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        checks = launch.run_checks(db)
    finally:
        db.close()

    print(render(checks))
    sys.exit(1 if launch.blocking(checks) else 0)


if __name__ == "__main__":  # pragma: no cover
    main()
