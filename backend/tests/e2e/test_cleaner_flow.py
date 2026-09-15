"""The cleaner's path and the admin's, clicked through in a browser.

Sign up as a cleaner, set a service area, upload an ID, get reviewed by an
admin, then bid on a job. The point of doing it in a browser is the middle:
the moment clearance flips, the same screen has to stop saying "you can't bid"
and start accepting a price. That transition spans three roles and two screens,
which is more than an endpoint test sees.
"""

from __future__ import annotations

import re
import uuid

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")
expect = playwright_api.expect

pytestmark = pytest.mark.e2e


def _set_service_area(page) -> None:
    """Pick a town the way a cleaner does now.

    The form used to ask for latitude and longitude. It asks for a town, so
    these tests type one — which is the point of a browser test: the selector
    changing is the screen changing, and a test that still filled two hidden
    coordinate fields would pass against a form nobody can complete.
    """
    page.fill("#service-area-search", "Portland")
    page.get_by_test_id("place-portland").click()
    expect(page.get_by_test_id("service-area-chosen")).to_be_visible()

PASSWORD = "correct-horse-battery"

# A one-pixel PNG, so the upload is a real image rather than bytes that happen
# to be labelled one.
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


def _signup(page, base_url: str, role: str) -> str:
    email = f"{role}-{uuid.uuid4().hex[:10]}@example.com"
    page.goto(f"{base_url}/signup?role={role}")
    page.fill("#full_name", "Dana Rivers" if role == "cleaner" else "Property Owner")
    page.fill("#email", email)
    page.fill("#password", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url("**/dashboard")
    return email


def _log_in(page, base_url: str, email: str) -> None:
    page.goto(f"{base_url}/login")
    page.fill("#email", email)
    page.fill("#password", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url("**/dashboard")


def _post_a_turnover(page, base_url: str) -> None:
    """An owner posts a job in Portland, so it lands in the cleaner's radius."""
    _signup(page, base_url, "owner")

    page.goto(f"{base_url}/properties/new")
    page.fill("#nickname", "Seaside Cottage")
    page.fill("#address_line1", "1 Harbor Way")
    page.fill("#city", "Portland")
    page.fill("#state", "ME")
    page.fill("#postal_code", "04101")
    page.fill("#access_notes", "Lockbox on the rail, code 4417.")
    page.click("button[type=submit]")
    page.wait_for_url(re.compile(r"/properties/[0-9a-fA-F-]{36}$"))

    # The board filters by distance, so the property needs coordinates. The
    # owner form has no lat/lng fields, so set them the way geocoding will.
    page.goto(f"{base_url}/properties")
    expect(page.get_by_text("Seaside Cottage")).to_be_visible()


def _set_property_coordinates(db) -> None:
    from sqlalchemy import select

    from app.models import Property

    prop = db.execute(select(Property)).scalars().first()
    prop.lat = 43.6591
    prop.lng = -70.2568
    db.commit()


def test_a_cleaner_cannot_bid_until_a_human_clears_them(make_page, live_server, db) -> None:
    base_url = live_server
    owner_page, cleaner_page, admin_page = make_page(), make_page(), make_page()

    # --- an owner posts a job ---
    page = owner_page
    _post_a_turnover(page, base_url)
    _set_property_coordinates(db)
    page.goto(f"{base_url}/turnovers/new")
    page.fill("#checkout_at", "2027-03-14T11:00")
    page.fill("#checkin_at", "2027-03-14T16:00")
    page.fill("#budget", "145")
    page.click("button[type=submit]")
    page.wait_for_url(re.compile(r"/turnovers/[0-9a-fA-F-]{36}$"))

    # --- a cleaner signs up and sets a service area ---
    page = cleaner_page
    # A cleaner with no profile yet gets a 404 from /cleaner/profile, which the
    # app treats as "first visit" and the browser logs as a failed request.
    page.expected_errors.append("404 (Not Found)")
    _signup(page, base_url, "cleaner")
    expect(page.get_by_role("heading", name="Set up your profile")).to_be_visible()

    page.goto(f"{base_url}/cleaner/profile")
    _set_service_area(page)
    page.click("button[type=submit]")

    expect(page.get_by_role("heading", name="Not cleared to bid yet")).to_be_visible()
    expect(page.get_by_text("ID verification not started").first).to_be_visible()
    expect(page.get_by_text("background check not started").first).to_be_visible()

    # --- the job is visible, but the bid box is not ---
    page.goto(f"{base_url}/board")
    expect(page.get_by_role("heading", name="Seaside Cottage")).to_be_visible()
    expect(page.get_by_text("Same day")).to_be_visible()

    # This is the privacy boundary, checked in the rendered page rather than
    # only in the JSON: a cleaner who has not been awarded the job must not be
    # able to read the address or the lockbox code off the screen.
    body = page.inner_text("body")
    assert "4417" not in body, "the lockbox code was rendered on the board"
    assert "Harbor Way" not in body, "the street address was rendered on the board"

    expect(page.get_by_role("button", name="Place bid")).to_have_count(0)
    expect(page.get_by_role("link", name="Finish your profile").first).to_be_visible()

    # --- the cleaner uploads an ID and starts a background check ---
    page.goto(f"{base_url}/cleaner/profile")
    page.set_input_files(
        "input[type=file]",
        files=[{"name": "licence.png", "mimeType": "image/png", "buffer": PNG_BYTES}],
    )
    expect(page.get_by_text("licence.png")).to_be_visible()
    expect(page.get_by_text("ID verification under review").first).to_be_visible()

    page.get_by_role("button", name="Start my background check").click()
    expect(page.get_by_text(re.compile("Status: pending"))).to_be_visible()

    # --- an admin works the queue ---
    from app.core.security import hash_password
    from app.models import User, UserRole

    admin_email = f"admin-{uuid.uuid4().hex[:8]}@example.com"
    db.add(
        User(
            email=admin_email,
            hashed_password=hash_password(PASSWORD),
            full_name="Queue Admin",
            role=UserRole.ADMIN,
        )
    )
    db.commit()

    page = admin_page
    _log_in(page, base_url, admin_email)
    page.goto(f"{base_url}/admin/vetting")
    expect(page.get_by_role("heading", name="Dana Rivers")).to_be_visible()
    expect(page.get_by_text("Not cleared").first).to_be_visible()

    page.get_by_role("button", name="Approve ID").click()
    # Half-vetted is still not vetted, and the queue says so.
    expect(page.get_by_text("Not cleared").first).to_be_visible()

    page.get_by_role("button", name="Mark clear").click()
    # **`exact=True`, and it is the whole assertion.** The queue renders either
    # "Cleared" or "Not cleared", and Playwright's default text match is a
    # case-insensitive *substring* — so `get_by_text("Cleared")` matches the
    # not-cleared row too. This line passed before the click as readily as
    # after it: the one browser assertion that the trust gate actually flips
    # proved nothing, and would have gone on passing if "Mark clear" did
    # nothing at all.
    expect(page.get_by_text("Cleared", exact=True).first).to_be_visible()

    # --- back as the cleaner, the same screen now takes a price ---
    page = cleaner_page
    page.goto(f"{base_url}/board")
    expect(page.get_by_role("button", name="Place bid")).to_be_visible()

    page.fill("input[id^=price-]", "135")
    page.fill("textarea[id^=message-]", "I can be there by 11.")
    page.get_by_role("button", name="Place bid").click()

    expect(page.get_by_text(re.compile(r"Your bid:\s*\$135"))).to_be_visible()
    expect(page.get_by_role("button", name="Update bid")).to_be_visible()

    # And the address is still not on the page now that they have bid.
    body = page.inner_text("body")
    assert "4417" not in body
    assert "Harbor Way" not in body
