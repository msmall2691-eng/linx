"""Tapping "I'm on site" from the property, in a real browser.

**The part endpoint tests cannot reach.** They prove the server turns a reading
into a verdict; they cannot prove the browser ever takes one, that the tap
survives a refused permission, or that the answer arrives on the owner's
screen. Each of those fails silently: the button works, the job starts, and the
confirmation simply never appears.

Two runs of the same flow, because the interesting cases are the two
permissions a cleaner can give:

* **granted, at the property** — the owner sees it confirmed;
* **refused** — the job starts exactly the same and nobody is accused of
  anything. This is the one that matters: a location feature that quietly
  punishes people for saying no is a different product from the one described
  on the landing page.
"""

from __future__ import annotations

import re
import uuid

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")
expect = playwright_api.expect

pytestmark = pytest.mark.e2e

PASSWORD = "correct-horse-battery"
#: The fixture property's own coordinates, so "at the property" is a real
#: measurement rather than a number chosen to pass.
AT_THE_PROPERTY = {"latitude": 43.6591, "longitude": -70.2568}


def _signup(page, base_url: str, role: str, name: str) -> str:
    email = f"{role}-{uuid.uuid4().hex[:10]}@example.com"
    page.goto(f"{base_url}/signup?role={role}")
    page.fill("#full_name", name)
    page.fill("#email", email)
    page.fill("#password", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url("**/dashboard")
    return email


def _clear_for_work(db, email: str) -> None:
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
    prop.lat, prop.lng = AT_THE_PROPERTY["latitude"], AT_THE_PROPERTY["longitude"]
    db.commit()


def _book_a_job(make_page, base_url, db):
    """Owner posts, cleaner bids, owner accepts. Returns both pages and the URL."""
    owner_page, cleaner_page = make_page(), make_page()

    page = owner_page
    _signup(page, base_url, "owner", "Property Owner")
    page.goto(f"{base_url}/properties/new")
    page.fill("#nickname", "Seaside Cottage")
    page.fill("#address_line1", "1 Harbor Way")
    page.fill("#city", "Portland")
    page.fill("#state", "ME")
    page.fill("#postal_code", "04101")
    page.fill("#access_notes", "Lockbox on the rail, code 4417.")
    page.click("button[type=submit]")
    page.wait_for_url(re.compile(r"/properties/[0-9a-fA-F-]{36}$"))
    _place_coordinates(db)

    page.goto(f"{base_url}/turnovers/new")
    page.fill("#checkout_at", "2027-05-20T11:00")
    page.fill("#checkin_at", "2027-05-20T16:00")
    page.fill("#budget", "145")
    page.click("button[type=submit]")
    page.wait_for_url(re.compile(r"/turnovers/[0-9a-fA-F-]{36}$"))
    turnover_url = page.url

    page = cleaner_page
    page.expected_errors.append("404 (Not Found)")
    cleaner_email = _signup(page, base_url, "cleaner", "Dana Rivers")
    page.goto(f"{base_url}/cleaner/profile")
    page.fill("#service-area-search", "Portland")
    page.get_by_test_id("place-portland").click()
    expect(page.get_by_test_id("service-area-chosen")).to_be_visible()
    page.click("button[type=submit]")
    # A real synchronisation point, not a sleep: the profile row does not exist
    # until this submit lands, and `_clear_for_work` reads it.
    expect(page.get_by_role("heading", name="Not cleared to bid yet")).to_be_visible()
    _clear_for_work(db, cleaner_email)

    page.goto(f"{base_url}/board")
    expect(page.get_by_role("button", name="Place bid")).to_be_visible()
    page.fill("input[id^=price-]", "135")
    page.get_by_role("button", name="Place bid").click()
    expect(page.get_by_text(re.compile(r"Your bid:\s*\$135"))).to_be_visible()

    page = owner_page
    page.goto(turnover_url)
    page.get_by_test_id("accept-bid").click()
    expect(page.get_by_test_id("award-panel")).to_be_visible()

    return owner_page, cleaner_page, turnover_url


def test_arriving_at_the_property_shows_the_owner_it_was_confirmed(
    make_page, live_server, db
) -> None:
    owner_page, cleaner_page, turnover_url = _book_a_job(make_page, live_server, db)

    # The cleaner's phone, allowed and standing at the house.
    cleaner_page.context.grant_permissions(["geolocation"])
    cleaner_page.context.set_geolocation(AT_THE_PROPERTY)

    cleaner_page.goto(f"{live_server}/jobs")
    # Said on the screen where it happens, not in a policy page somebody has
    # to go looking for.
    expect(cleaner_page.get_by_test_id("arrival-note")).to_contain_text("say no")

    cleaner_page.get_by_test_id("start-job").click()
    expect(cleaner_page.get_by_test_id("start-job")).to_have_count(0)
    expect(cleaner_page.get_by_test_id("arrival-confirmed")).to_be_visible()

    # The whole point: it reaches the person who wanted to know.
    owner_page.goto(turnover_url)
    expect(owner_page.get_by_test_id("arrival-confirmed")).to_be_visible()


def test_refusing_the_permission_starts_the_job_and_accuses_nobody(
    make_page, live_server, db
) -> None:
    """**The test worth having.**

    Everything about this feature is a choice about what happens when somebody
    says no. The job has to start normally, and the owner's screen must show
    the arrival with *no* note at all — not a warning, not an "unconfirmed"
    badge, because a refused browser prompt is the ordinary case and a line
    about it on every job reads as a warning about a person.
    """
    owner_page, cleaner_page, turnover_url = _book_a_job(make_page, live_server, db)

    # Nothing granted: `getCurrentPosition` fails or never answers, and
    # `whereAmI` resolves to null either way.
    cleaner_page.goto(f"{live_server}/jobs")
    cleaner_page.get_by_test_id("start-job").click()
    expect(cleaner_page.get_by_test_id("start-job")).to_have_count(0)

    owner_page.goto(turnover_url)
    panel = owner_page.get_by_test_id("job-progress")
    expect(panel).to_contain_text("On site")
    expect(owner_page.get_by_test_id("arrival-confirmed")).to_have_count(0)
    expect(owner_page.get_by_test_id("arrival-away")).to_have_count(0)
    body = owner_page.inner_text("body").lower()
    for accusation in ("unconfirmed", "could not verify", "not verified"):
        assert accusation not in body, (
            f"a refused permission is being reported as {accusation!r}"
        )
