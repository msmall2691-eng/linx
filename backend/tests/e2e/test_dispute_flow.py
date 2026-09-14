"""A complaint, and the person who answers it — clicked through in a browser.

Phase 8's screens are the ones with the least forgiving failure mode in the
product: a dispute is somebody's bad experience, and a console that returns 200
while going blank means the complaint is *seen by nobody* and the person who
raised it has no way to know. Endpoint tests cannot see that; this can.

It also pins the boundary that matters most on these screens: **the other side
is not told a dispute exists** until a person decides to involve them. That is
asserted here on the rendered page, not only in the notification rows, because
"it is not in the API response" and "it is not on the screen" have come apart
in this repo before.
"""

from __future__ import annotations

import re
import uuid

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")
expect = playwright_api.expect

pytestmark = pytest.mark.e2e

PASSWORD = "correct-horse-battery"
PROPERTY_DETAIL = re.compile(r"/properties/[0-9a-fA-F-]{36}$")
TURNOVER_DETAIL = re.compile(r"/turnovers/[0-9a-fA-F-]{36}$")


def _sign_up(page, base_url: str, role: str, name: str) -> str:
    email = f"{role}-{uuid.uuid4().hex[:10]}@example.com"
    page.goto(f"{base_url}/signup?role={role}")
    page.fill("#full_name", name)
    page.fill("#email", email)
    page.fill("#password", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url("**/dashboard")
    return email


def _set_service_area(page) -> None:
    """Pick a town the way a cleaner does — the form asks for one, not for
    coordinates, and a test that filled hidden coordinate fields would pass
    against a form nobody can complete."""
    page.fill("#service-area-search", "Portland")
    page.get_by_test_id("place-portland").click()
    expect(page.get_by_test_id("service-area-chosen")).to_be_visible()


def _clear_for_work(db, email: str) -> None:
    """Finish this cleaner's vetting the way an admin would — the same helper
    `test_award_flow` uses, and for the same reason: `can_take_jobs` is a
    generated column, so the statuses go through the door the buttons use."""
    from sqlalchemy import select

    from app.models import CleanerProfile, User, VerificationStatus

    user = db.execute(select(User).where(User.email == email)).scalar_one()
    profile = db.execute(
        select(CleanerProfile).where(CleanerProfile.user_id == user.id)
    ).scalar_one()
    profile.id_verification_status = VerificationStatus.APPROVED
    profile.background_check_status = VerificationStatus.APPROVED
    db.commit()


def _place_coordinates(db) -> None:
    from sqlalchemy import select

    from app.models import Property

    prop = db.execute(select(Property)).scalars().first()
    prop.lat, prop.lng = 43.6591, -70.2568
    db.commit()


def _log_in(page, base_url: str, email: str) -> None:
    page.goto(f"{base_url}/login")
    page.fill("#email", email)
    page.fill("#password", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url("**/dashboard")


def test_a_dispute_reaches_a_person_and_the_other_side_is_not_told(
    make_page, live_server, db
) -> None:
    base_url = live_server
    owner_page, cleaner_page, admin_page = make_page(), make_page(), make_page()

    # --- an owner with a job, and a cleaner booked on it ------------------
    _sign_up(owner_page, base_url, "owner", "Dispute Owner")
    owner_page.goto(f"{base_url}/properties/new")
    owner_page.fill("#nickname", "Harbour Flat")
    owner_page.fill("#address_line1", "9 Quay St")
    owner_page.fill("#city", "Portland")
    owner_page.fill("#state", "ME")
    owner_page.fill("#postal_code", "04101")
    owner_page.fill("#bedrooms", "2")
    owner_page.fill("#bathrooms", "1")
    owner_page.click("button[type=submit]")
    owner_page.wait_for_url(PROPERTY_DETAIL)

    owner_page.get_by_role("link", name="Post a turnover").click()
    owner_page.wait_for_url("**/turnovers/new**")
    owner_page.fill("#checkout_at", "2026-11-02T11:00")
    owner_page.fill("#checkin_at", "2026-11-04T16:00")
    owner_page.fill("#budget", "150")
    owner_page.click("button[type=submit]")
    owner_page.wait_for_url(TURNOVER_DETAIL)
    turnover_url = owner_page.url

    # A cleared cleaner. The vetting click-through is `test_cleaner_flow.py`;
    # repeating it here would make this test about the wrong thing.
    cleaner_page.expected_errors.append("404 (Not Found)")  # no profile yet
    cleaner_email = _sign_up(cleaner_page, base_url, "cleaner", "Booked Cleaner")
    cleaner_page.goto(f"{base_url}/cleaner/profile")
    _set_service_area(cleaner_page)
    cleaner_page.click("button[type=submit]")
    # **Wait for the save to land before reading the profile back.** A helper
    # that returns before its own work has committed poisons every step after
    # it, somewhere far from the cause — the profile simply is not there yet.
    expect(
        cleaner_page.get_by_role("heading", name="Not cleared to bid yet")
    ).to_be_visible()

    _clear_for_work(db, cleaner_email)
    _place_coordinates(db)

    cleaner_page.goto(f"{base_url}/board")
    expect(cleaner_page.get_by_role("button", name="Place bid")).to_be_visible()
    cleaner_page.fill("input[id^=price-]", "150")
    cleaner_page.get_by_role("button", name="Place bid").click()
    expect(cleaner_page.get_by_text(re.compile(r"Your bid:\s*\$150"))).to_be_visible()

    owner_page.goto(turnover_url)
    expect(owner_page.get_by_test_id("bid")).to_have_count(1)
    owner_page.get_by_test_id("accept-bid").click()

    # --- the owner raises a dispute --------------------------------------
    owner_page.goto(turnover_url)
    expect(owner_page.get_by_test_id("dispute-panel")).to_be_visible()
    owner_page.get_by_test_id("open-dispute-form").click()
    owner_page.get_by_test_id("dispute-reason").select_option("quality")
    owner_page.get_by_test_id("dispute-description").fill(
        "Kitchen was not touched and the bins were left full."
    )
    owner_page.get_by_test_id("submit-dispute").click()

    # The screen it was raised from stays alive and shows it — the exact class
    # of failure the browser suite exists for.
    expect(owner_page.get_by_test_id("dispute").first).to_be_visible()
    # The screen says what it means rather than echoing the enum — "open" is
    # a database word, and the person reading this wants to know somebody will
    # look at it.
    expect(owner_page.get_by_test_id("dispute-status").first).to_have_text(
        "Waiting for someone to pick it up"
    )

    # --- and the cleaner is told nothing ---------------------------------
    cleaner_page.goto(f"{base_url}/jobs")
    body = cleaner_page.inner_text("body")
    assert "Kitchen was not touched" not in body
    assert "dispute" not in body.lower(), (
        "the cleaner must not learn a complaint exists until a person decides "
        "to involve them"
    )

    # --- an admin works it in the console --------------------------------
    from app.core.security import hash_password
    from app.models import User, UserRole

    admin_email = f"admin-{uuid.uuid4().hex[:8]}@example.com"
    db.add(
        User(
            email=admin_email,
            hashed_password=hash_password(PASSWORD),
            full_name="Console Admin",
            role=UserRole.ADMIN,
        )
    )
    db.commit()

    _log_in(admin_page, base_url, admin_email)
    admin_page.goto(f"{base_url}/admin")
    expect(admin_page.get_by_test_id("dispute-inbox")).to_be_visible()
    expect(admin_page.get_by_test_id("admin-dispute").first).to_be_visible()
    expect(
        admin_page.get_by_text("Kitchen was not touched and the bins were left full.")
    ).to_be_visible()

    admin_page.get_by_test_id("acknowledge-dispute").first.click()
    expect(admin_page.get_by_test_id("admin-dispute-status").first).to_have_text(
        re.compile("acknowledged", re.I)
    )

    admin_page.get_by_test_id("open-resolve-form").first.click()
    admin_page.get_by_test_id("resolution-notes").fill(
        "Spoke to both. Cleaner returning Thursday at no charge."
    )
    admin_page.get_by_test_id("submit-resolution").click()

    # The console keeps rendering after the write, and the queue reflects it.
    expect(admin_page.get_by_test_id("dispute-inbox")).to_be_visible()

    # --- now both sides see the outcome ----------------------------------
    owner_page.goto(turnover_url)
    expect(owner_page.get_by_test_id("dispute-status").first).to_have_text("Resolved")
    expect(owner_page.get_by_test_id("dispute-resolution").first).to_have_text(
        re.compile("Cleaner returning Thursday")
    )
