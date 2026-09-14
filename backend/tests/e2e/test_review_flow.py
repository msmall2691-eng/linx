"""The delayed reveal, watched from both screens at once.

The endpoint tests prove the server withholds a review. What only a browser can
prove is that **the screen withholds it too** — that an owner's page, sitting
open, does not grow a review the moment the cleaner writes one, and does not
acquire any other tell that a review now exists.

That is the failure the whole phase is about and it is a rendering failure as
much as an API one: a "1 review pending" badge, a changed empty state, a count
that ticks — any of those hands over the one fact the delay exists to withhold,
while the review itself stays technically hidden.

So this test holds two browser contexts open, reads one side's page before and
after the other side writes, and asserts the visible text did not change.
"""

from __future__ import annotations

import re
import uuid

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")
expect = playwright_api.expect

pytestmark = pytest.mark.e2e

PASSWORD = "correct-horse-battery"
PORTLAND = ("43.6591", "-70.2568")


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


def _place_coordinates(db, nickname: str) -> None:
    from sqlalchemy import select

    from app.models import Property

    prop = db.execute(
        select(Property).where(Property.nickname == nickname)
    ).scalars().first()
    prop.lat, prop.lng = 43.6591, -70.2568
    db.commit()


def test_neither_side_sees_the_other_until_both_have_written(
    make_page, live_server, db
) -> None:
    base_url = live_server
    owner_page, cleaner_page = make_page(), make_page()

    # --- a job, taken all the way to done ---
    page = owner_page
    _signup(page, base_url, "owner", "Ada Owner")

    page.goto(f"{base_url}/properties/new")
    page.fill("#nickname", "Lighthouse Cottage")
    page.fill("#address_line1", "4 Beacon Rd")
    page.fill("#city", "Portland")
    page.fill("#state", "ME")
    page.fill("#postal_code", "04101")
    page.click("button[type=submit]")
    page.wait_for_url(re.compile(r"/properties/[0-9a-fA-F-]{36}$"))
    _place_coordinates(db, "Lighthouse Cottage")

    page.goto(f"{base_url}/turnovers/new")
    page.fill("#checkout_at", "2027-07-04T11:00")
    page.fill("#checkin_at", "2027-07-04T16:00")
    page.fill("#budget", "160")
    page.click("button[type=submit]")
    page.wait_for_url(re.compile(r"/turnovers/[0-9a-fA-F-]{36}$"))
    turnover_url = page.url

    page = cleaner_page
    page.expected_errors.append("404 (Not Found)")  # first visit, no profile yet
    cleaner_email = _signup(page, base_url, "cleaner", "Kit Marlow")
    page.goto(f"{base_url}/cleaner/profile")
    page.fill("#service_lat", PORTLAND[0])
    page.fill("#service_lng", PORTLAND[1])
    page.fill("#service_radius_miles", "30")
    page.click("button[type=submit]")
    expect(page.get_by_role("heading", name="Not cleared to bid yet")).to_be_visible()
    _clear_for_work(db, cleaner_email)

    page.goto(f"{base_url}/board")
    expect(page.get_by_role("button", name="Place bid")).to_be_visible()
    page.fill("input[id^=price-]", "150")
    page.get_by_role("button", name="Place bid").click()
    expect(page.get_by_text(re.compile(r"Your bid:\s*\$150"))).to_be_visible()

    page = owner_page
    page.goto(turnover_url)
    page.get_by_test_id("accept-bid").click()
    expect(page.get_by_test_id("award-panel")).to_be_visible()
    # Nothing to review yet — the job has not been done.
    expect(page.get_by_test_id("review-panel")).to_have_count(0)

    page = cleaner_page
    page.goto(f"{base_url}/jobs")
    page.get_by_test_id("complete-job").click()
    expect(page.get_by_test_id("job-done")).to_be_visible()

    # --- the cleaner reviews first ---
    expect(page.get_by_test_id("review-panel")).to_be_visible()
    page.get_by_test_id("review-star-2").click()
    page.get_by_test_id("review-text").fill("Place was left in a state.")
    page.get_by_test_id("submit-review").click()

    # Their own comes back, marked as held. The form is gone — no edits.
    expect(page.get_by_test_id("review-held")).to_be_visible()
    expect(page.get_by_test_id("submit-review")).to_have_count(0)
    expect(page.get_by_test_id("review")).to_have_count(1)

    # --- and the owner's screen learns nothing ---
    page = owner_page
    page.goto(turnover_url)
    expect(page.get_by_test_id("review-panel")).to_be_visible()
    before = page.get_by_test_id("review-panel").inner_text()

    assert "state" not in before.lower() or "Place was left" not in before, (
        "the cleaner's review text leaked onto the owner's screen"
    )
    expect(page.get_by_test_id("review")).to_have_count(0)
    expect(page.get_by_test_id("submit-review")).to_be_visible()

    # Reload: still nothing. The panel must not acquire a tell — no badge, no
    # count, no altered wording — because that is the fact the delay withholds.
    page.reload()
    assert page.get_by_test_id("review-panel").inner_text() == before, (
        "the owner's panel changed once the cleaner had written, which tells "
        "them a review exists and how soon to get theirs in"
    )

    # --- the owner writes, and both open together ---
    page.get_by_test_id("review-star-4").click()
    page.get_by_test_id("review-text").fill("Fair enough, we left it late.")
    page.get_by_test_id("submit-review").click()

    expect(page.get_by_test_id("review")).to_have_count(2)
    expect(page.get_by_test_id("review-held")).to_have_count(0)
    assert "Place was left in a state." in page.get_by_test_id("review-panel").inner_text()

    # --- and the cleaner's side opens too ---
    page = cleaner_page
    page.goto(f"{base_url}/jobs")
    expect(page.get_by_test_id("review")).to_have_count(2)
    assert "Fair enough, we left it late." in page.get_by_test_id("review-panel").inner_text()
    expect(page.get_by_test_id("review-held")).to_have_count(0)
