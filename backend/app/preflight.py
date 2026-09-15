"""Startup preflight: check the environment, then wait for the database.

Run before migrations. It exists because the two ways this container fails to
boot both used to surface as a raw traceback — a SQLAlchemy `OperationalError`
or a pydantic `ValidationError` buried in a stack — and neither says what to
actually do about it. On a hosted deploy the log is all anyone has, so it has
to name the variable that is wrong.

The wait matters just as much as the check. A managed Postgres is often not
accepting connections at the instant the app container starts, especially on a
project's first deploy. Migrating immediately turns that ordinary startup race
into a failed deployment, so this retries for a bounded window first and only
gives up when the database is genuinely unreachable.
"""

from __future__ import annotations

import os
import sys
import time

#: How long to wait for the database before giving up.
CONNECT_TIMEOUT_SECONDS = float(os.environ.get("LINX_DB_WAIT_SECONDS", "60"))
#: Delay between connection attempts.
RETRY_INTERVAL_SECONDS = 2.0
#: Per-attempt TCP connect timeout.
#:
#: Without this the overall wait is not actually bounded: a host that silently
#: drops packets (a wrong address, a firewall) leaves a single connect hanging
#: for the OS default — minutes — so the deadline below is never even checked.
#: A refused connection fails fast; a black-holed one does not.
CONNECT_ATTEMPT_TIMEOUT_SECONDS = 5


def _fail(message: str) -> None:
    print(f"\nlinx preflight: {message}\n", file=sys.stderr)
    sys.exit(1)


def _load_settings():
    """Import settings, turning a config error into an actionable message."""
    try:
        from app.config import settings
    except Exception as exc:  # noqa: BLE001 - any config problem must be legible
        message = str(exc)
        if "SECRET_KEY" in message:
            _fail(
                "SECRET_KEY is missing, too short, or still the development "
                "placeholder, while ENVIRONMENT=production.\n"
                "  Anybody holding an ordinary token can forge an admin one "
                "from a guessable signing key.\n"
                "  Set it on the service to a real value:\n"
                '    python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        _fail(f"configuration is invalid.\n  {message}")
    return settings


def _wait_for_database(settings) -> None:
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import SQLAlchemyError

    # A short-lived engine of its own: the app's pooled engine is built for
    # serving traffic, not for probing a database that may not be up yet.
    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": CONNECT_ATTEMPT_TIMEOUT_SECONDS},
    )

    deadline = time.monotonic() + CONNECT_TIMEOUT_SECONDS
    attempt = 0
    last_error: Exception | None = None

    while time.monotonic() < deadline:
        attempt += 1
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            if attempt > 1:
                print(f"Database reachable after {attempt} attempts.")
            engine.dispose()
            return
        except SQLAlchemyError as exc:
            last_error = exc
            print(
                f"Database not ready yet (attempt {attempt}); "
                f"retrying in {RETRY_INTERVAL_SECONDS:.0f}s…"
            )
            time.sleep(RETRY_INTERVAL_SECONDS)

    engine.dispose()
    _fail(
        f"could not reach the database within {CONNECT_TIMEOUT_SECONDS:.0f}s.\n"
        "  Check DATABASE_URL on the service — on Railway it should reference the\n"
        "  Postgres service, e.g. ${{Postgres.DATABASE_URL}}, not a literal host.\n"
        f"  Last error: {last_error}"
    )


def main() -> None:
    if not os.environ.get("DATABASE_URL"):
        _fail(
            "DATABASE_URL is not set.\n"
            "  Add a Postgres service and reference it from this service's variables:\n"
            "    DATABASE_URL = ${{Postgres.DATABASE_URL}}"
        )

    settings = _load_settings()
    print(f"Preflight: environment={settings.environment}, region={settings.region_name}")
    _wait_for_database(settings)
    print("Preflight OK.")


if __name__ == "__main__":
    main()
