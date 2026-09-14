"""The startup preflight.

Most of this is I/O and is verified by running the real container (a database
that arrives late, an unreachable host, a missing key). What is worth pinning
here is the contract the entrypoint depends on: a missing variable exits
non-zero, and the message names the variable rather than burying it in a stack.
"""

from __future__ import annotations

import pytest

from app import preflight


def test_a_missing_database_url_exits_with_an_actionable_message(
    monkeypatch, capsys
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(SystemExit) as exit_info:
        preflight.main()

    # Non-zero, so `set -e` in the entrypoint stops before migrations run.
    assert exit_info.value.code == 1

    message = capsys.readouterr().err
    assert "DATABASE_URL is not set" in message
    # Says what to do, not just what went wrong.
    assert "Postgres.DATABASE_URL" in message


def test_each_connection_attempt_is_bounded(monkeypatch) -> None:
    """Without a per-attempt timeout the overall wait is not really bounded.

    A host that drops packets rather than refusing leaves one connect hanging
    for the OS default, so the deadline is never reached and the deploy hangs
    instead of failing with a readable message.
    """
    captured: dict = {}

    def fake_create_engine(url, **kwargs):
        captured.update(kwargs)
        raise AssertionError("stop here; only the engine arguments matter")

    monkeypatch.setattr("sqlalchemy.create_engine", fake_create_engine)

    from app.config import settings

    with pytest.raises(AssertionError):
        preflight._wait_for_database(settings)

    assert captured["connect_args"]["connect_timeout"] == (
        preflight.CONNECT_ATTEMPT_TIMEOUT_SECONDS
    )
