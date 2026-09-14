"""The owner's path, clicked through in a browser.

Sign up, add a property, post a turnover, watch the urgency ladder appear, then
cancel it. Not "the button renders" — every step here asserts on what a person
would actually see after the click.
"""

from __future__ import annotations

import re
import uuid

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")
expect = playwright_api.expect

pytestmark = pytest.mark.e2e

PASSWORD = "correct-horse-battery"

# Badge state is asserted through `data-testid`, never through free text.
# Playwright matches text by substring and case-insensitively, so
# `get_by_text("Draft")` also matches the sentence "Nobody can see this draft
# yet." — a strict-mode violation that only shows up once both have rendered,
# which made it a race rather than an honest failure.
STATUS_BADGE = "status-badge"
URGENCY_BADGE = "urgency-badge"

# Wait for the *detail* route, matched by its id segment.
#
# A glob like "**/turnovers/**" also matches "/turnovers/new" — the page being
# submitted — so the wait returns instantly and the next `goto` can abort the
# create request that is still in flight. The row is never written, and the test
# fails somewhere further along with no sign of why.
PROPERTY_DETAIL = re.compile(r"/properties/[0-9a-fA-F-]{36}$")
TURNOVER_DETAIL = re.compile(r"/turnovers/[0-9a-fA-F-]{36}$")


def _signup_owner(page, base_url: str) -> str:
    email = f"owner-{uuid.uuid4().hex[:10]}@example.com"
    page.goto(f"{base_url}/signup?role=owner")
    page.fill("#full_name", "Click Through")
    page.fill("#email", email)
    page.fill("#password", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url("**/dashboard")
    return email


def _add_property(page, base_url: str, nickname: str = "Seaside Cottage") -> None:
    page.goto(f"{base_url}/properties/new")
    page.fill("#nickname", nickname)
    page.fill("#address_line1", "1 Harbor Way")
    page.fill("#city", "Portland")
    page.fill("#state", "ME")
    page.fill("#postal_code", "04101")
    page.fill("#bedrooms", "2")
    page.fill("#bathrooms", "1.5")
    page.fill("#access_notes", "Lockbox on the rail, code 4417.")
    page.click("button[type=submit]")

    # Wait on the thing that can only be true once the row exists: the detail
    # route, and the detail page rendered from it. A helper that returns before
    # its own work has landed poisons every step after it, somewhere far from
    # the cause.
    page.wait_for_url(PROPERTY_DETAIL)
    expect(page.get_by_role("heading", name=nickname)).to_be_visible()


def test_a_new_owner_can_go_from_signup_to_a_posted_turnover(page, live_server) -> None:
    _signup_owner(page, live_server)

    # An owner with nothing yet is pointed at the first useful thing to do.
    expect(page.get_by_role("heading", name="Start with a property")).to_be_visible()
    page.get_by_role("link", name="Add a property").click()
    page.wait_for_url("**/properties/new")

    _add_property(page, live_server)
    expect(page.get_by_role("heading", name="Seaside Cottage")).to_be_visible()
    expect(page.get_by_text("Lockbox on the rail, code 4417.")).to_be_visible()

    page.get_by_role("link", name="Post a turnover").click()
    page.wait_for_url("**/turnovers/new**")

    # 11am and 4pm on one local day — the top of the urgency ladder.
    page.fill("#checkout_at", "2026-10-14T11:00")
    page.fill("#checkin_at", "2026-10-14T16:00")
    page.fill("#budget", "145")
    page.fill("#notes", "Guests had a dog.")

    # The ladder is visible while the owner can still change the times, which is
    # the only moment the information is worth anything.
    expect(page.get_by_test_id(URGENCY_BADGE)).to_have_text("Same day")
    expect(page.get_by_text("5 hours between guests")).to_be_visible()

    page.click("button[type=submit]")
    page.wait_for_url(TURNOVER_DETAIL)

    expect(page.get_by_text("Same-day turnaround")).to_be_visible()
    expect(page.get_by_test_id(URGENCY_BADGE)).to_have_text("Same day")
    expect(page.get_by_test_id(STATUS_BADGE)).to_have_text("Taking bids")
    expect(page.get_by_text("$145")).to_be_visible()
    expect(page.get_by_text("Guests had a dog.")).to_be_visible()

    # Times render in the region's zone, not the browser's and not UTC.
    expect(page.get_by_text("Wed, Oct 14, 11:00 AM")).to_be_visible()
    expect(page.get_by_text("Wed, Oct 14, 4:00 PM")).to_be_visible()

    page.goto(f"{live_server}/turnovers")
    expect(page.get_by_text("Seaside Cottage")).to_be_visible()
    expect(page.get_by_test_id(URGENCY_BADGE)).to_have_text("Same day")


def test_a_draft_is_not_posted_until_the_owner_posts_it(page, live_server) -> None:
    _signup_owner(page, live_server)
    _add_property(page, live_server)

    page.goto(f"{live_server}/turnovers/new")
    page.fill("#checkout_at", "2026-11-20T10:00")
    page.get_by_label("Post it to cleaners now").uncheck()
    page.click("button[type=submit]")
    page.wait_for_url(TURNOVER_DETAIL)

    expect(page.get_by_test_id(STATUS_BADGE)).to_have_text("Draft")
    expect(page.get_by_text("Nobody can see this draft yet.")).to_be_visible()

    page.get_by_role("button", name="Post it to cleaners").click()
    expect(page.get_by_test_id(STATUS_BADGE)).to_have_text("Taking bids")
    expect(page.get_by_text("Nobody can see this draft yet.")).to_have_count(0)


def test_cancelling_leaves_the_page_readable(page, live_server) -> None:
    """Regression: the cancel response once dropped the nested property.

    The request returned 200, the row was cancelled correctly, and the screen
    went blank on the next render — nothing in the server log, nothing in any
    endpoint test. This is the test that catches that shape of bug.
    """
    _signup_owner(page, live_server)
    _add_property(page, live_server)

    page.goto(f"{live_server}/turnovers/new")
    page.fill("#checkout_at", "2026-12-02T10:00")
    page.click("button[type=submit]")
    page.wait_for_url(TURNOVER_DETAIL)

    page.get_by_role("button", name="Cancel this turnover").click()
    page.fill("#reason", "Guest extended their stay.")
    page.get_by_role("button", name="Cancel this turnover").click()

    # The page still renders, and it says what happened.
    expect(page.get_by_role("heading", name="Seaside Cottage")).to_be_visible()
    expect(page.get_by_test_id(STATUS_BADGE)).to_have_text("Cancelled")
    expect(page.get_by_text("Guest extended their stay.")).to_be_visible()

    # An urgency badge on a dead job is an alarm about nothing.
    expect(page.get_by_test_id(URGENCY_BADGE)).to_have_count(0)


def test_archiving_is_refused_while_a_turnover_is_still_scheduled(
    page, live_server
) -> None:
    """Guardrail 3, from the owner's side: the refusal has to be visible.

    A property that quietly vanished while a cleaner was still booked to show up
    there is the silent kind of breakage. The owner sees why instead.
    """
    _signup_owner(page, live_server)
    _add_property(page, live_server)

    page.goto(f"{live_server}/turnovers/new")
    page.fill("#checkout_at", "2026-12-15T10:00")
    page.click("button[type=submit]")
    page.wait_for_url(TURNOVER_DETAIL)

    page.goto(f"{live_server}/properties")
    page.get_by_text("Seaside Cottage").click()
    page.wait_for_url(PROPERTY_DETAIL)

    # The browser logs the deliberate 409 as a console error.
    page.expected_errors.append("409 (Conflict)")

    page.get_by_role("button", name="Archive this property").click()
    expect(page.get_by_role("alert")).to_contain_text("still scheduled")

    # And the property is still there.
    page.goto(f"{live_server}/properties")
    expect(page.get_by_text("Seaside Cottage")).to_be_visible()


def test_a_protected_route_sends_you_to_login_and_back(page, live_server) -> None:
    """Ask for a page while logged out, and land on it after logging in.

    This round trip is the part of routing that is easiest to break without
    noticing: the guard stashes where you were heading in router state, and the
    login screen reads it back. Nothing else in the suite exercises
    `useLocation`, `Navigate state=`, or `useNavigate` with state, which makes
    it the behaviour worth pinning across a router upgrade.
    """
    email = _signup_owner(page, live_server)
    _add_property(page, live_server)

    # Wait on being logged out, not on where logout lands. Signing out from a
    # guarded page re-renders the guard first, so it redirects to login before
    # the nav's own "go home" arrives — a detail of that race, not a promise.
    page.get_by_role("button", name="Log out").click()
    expect(page.get_by_role("link", name="Log in")).to_be_visible()

    # Ask for an owner-only page while logged out.
    page.goto(f"{live_server}/turnovers")
    page.wait_for_url("**/login")

    page.fill("#email", email)
    page.fill("#password", PASSWORD)
    page.click("button[type=submit]")

    # Back to where you were headed, not dumped on the dashboard.
    page.wait_for_url("**/turnovers")
    expect(page.get_by_role("heading", name="Turnovers")).to_be_visible()
