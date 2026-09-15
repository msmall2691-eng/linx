"""The launch checklist, on the screen a person actually reads.

`test_launch.py` asserts the checks are right. This asserts somebody can see
them — which is a separate question, and one this suite exists for: phase 8
shipped a dispute panel that was never mounted on any screen while every
endpoint test passed.

The assertion that matters is the last one. A readiness list is most dangerous
when it looks complete, so the screen has to show the items nobody can check
rather than quietly counting them as done.
"""

from __future__ import annotations

import uuid

import pytest

# **`importorskip`, not a plain import.** The backend CI job installs
# `requirements-dev.txt`, which has no playwright, and runs `pytest -q` over
# this whole tree — so a module-scope `from playwright...` fails *collection*
# and takes the entire backend suite down with it, which is exactly what it
# did. Every other file in this directory already does it this way.
playwright_api = pytest.importorskip("playwright.sync_api")
expect = playwright_api.expect

pytestmark = pytest.mark.e2e

PASSWORD = "correct-horse-battery"


def _log_in(page, base_url: str, email: str) -> None:
    page.goto(f"{base_url}/login")
    page.fill("#email", email)
    page.fill("#password", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url("**/dashboard")


def test_the_console_shows_what_nobody_has_checked(make_page, live_server, db) -> None:
    base_url = live_server
    admin_page = make_page()

    from app.core.security import hash_password
    from app.models import User, UserRole

    email = f"admin-{uuid.uuid4().hex[:8]}@example.com"
    db.add(
        User(
            email=email,
            hashed_password=hash_password(PASSWORD),
            full_name="Console Admin",
            role=UserRole.ADMIN,
        )
    )
    db.commit()

    _log_in(admin_page, base_url, email)
    admin_page.goto(f"{base_url}/admin")

    expect(admin_page.get_by_test_id("launch-readiness")).to_be_visible()

    # The dev test environment has no mail host, no Stripe key and no cron
    # service, so the summary must say so rather than reassure.
    expect(admin_page.get_by_test_id("launch-summary")).to_contain_text("blocking")

    # **The whole design, on screen.** Three items no code can check are
    # rendered as their own state — not as passes, and not hidden.
    expect(
        admin_page.get_by_test_id("launch-check-unverifiable")
    ).to_have_count(3)
    body = admin_page.inner_text("body")
    assert "Needs a person" in body
    assert "personal SSN" in body, (
        "the line this project exists to hold is not on the screen that decides "
        "whether to launch"
    )

    # Passing items are folded away until asked for: a list that leads with what
    # is already done is a list somebody stops reading.
    ready_before = admin_page.get_by_test_id("launch-check-ready").count()
    admin_page.get_by_test_id("toggle-launch-detail").click()
    assert admin_page.get_by_test_id("launch-check-ready").count() > ready_before
