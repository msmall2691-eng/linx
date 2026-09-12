"""Fixtures for the browser-driven tests.

These drive the real app in a real browser: a built frontend, served by the
real backend, clicked through by a real person's motions. They exist because a
whole class of bug is invisible to endpoint tests — an action that returns 200
while the screen it belongs to goes blank. One of those shipped in this repo's
first pass at phase 2 and was caught here, not by the 121 tests that were
already green.

They are **opt-in**. Running them needs a built frontend and a browser, so they
skip unless both are present:

    cd frontend && npm run build
    cd ../backend && pip install -r requirements-e2e.txt && playwright install chromium
    LINX_E2E=1 pytest tests/e2e
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
FRONTEND_BUILD = BACKEND_DIR / "static" / "index.html"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _skip_reason() -> str | None:
    if os.environ.get("LINX_E2E") != "1":
        return "browser tests are opt-in; set LINX_E2E=1"
    if not FRONTEND_BUILD.is_file():
        return f"no frontend build at {FRONTEND_BUILD} — run `npm run build` in frontend/"
    try:
        import playwright  # noqa: F401
    except ImportError:
        return "playwright is not installed; pip install -r requirements-e2e.txt"
    return None


@pytest.fixture(scope="session")
def live_server(migrated_database) -> Iterator[str]:
    """Run the real app against the test database and yield its base URL."""
    reason = _skip_reason()
    if reason:
        pytest.skip(reason)

    port = _free_port()
    env = {
        **os.environ,
        "DATABASE_URL": os.environ["DATABASE_URL"],
        "ENVIRONMENT": "test",
        "SECRET_KEY": os.environ.get("SECRET_KEY", "test-only-secret-key"),
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=BACKEND_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = (process.stdout.read() or b"").decode(errors="replace")
            raise RuntimeError(f"the app exited before it was ready:\n{output}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.2)
    else:
        process.kill()
        raise RuntimeError("the app did not start within 30 seconds")

    try:
        yield base_url
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - shutdown safety net
            process.kill()


@pytest.fixture(scope="session")
def browser():
    reason = _skip_reason()
    if reason:
        pytest.skip(reason)

    from playwright.sync_api import sync_playwright

    # Honour an explicit binary when the environment ships one that does not
    # match playwright's expected build (CI installs a matching one instead).
    executable = os.environ.get("LINX_CHROMIUM_PATH")
    launch_kwargs = {"executable_path": executable} if executable else {}

    with sync_playwright() as p:
        instance = p.chromium.launch(**launch_kwargs)
        yield instance
        instance.close()


@pytest.fixture
def make_page(browser, live_server):
    """Open an independent browser context, each with its own session.

    A test that wears more than one hat gets one page per role rather than
    logging in and out of a single session. Contexts have separate storage, so
    there is no signed-in user to dislodge and no race against the async
    session restore — and it matches reality, where these are three different
    people at three different computers.
    """
    contexts = []
    pages = []

    def _make_page():
        context = browser.new_context(viewport={"width": 1100, "height": 900})
        contexts.append(context)
        page = context.new_page()

        page.errors = []
        page.on("pageerror", lambda e: page.errors.append(f"pageerror: {e}"))
        page.on(
            "console",
            lambda m: page.errors.append(f"console.error: {m.text}")
            if m.type == "error"
            else None,
        )
        page.expected_errors = ["favicon"]
        pages.append(page)
        return page

    yield _make_page

    unexpected = [
        error
        for page in pages
        for error in page.errors
        if not any(x in error for x in page.expected_errors)
    ]
    for context in contexts:
        context.close()
    assert not unexpected, "browser reported errors:\n  " + "\n  ".join(unexpected)


@pytest.fixture
def page(make_page):
    """A single page, for tests that only need one role."""
    return make_page()
