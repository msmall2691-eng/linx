"""Adding a season of cleans to a home, clicked through in a browser.

The owner this exists for has **no booking calendar to connect** — a home never
has one — so every job was a separate trip through a form. This is the screen
that fixes that, and a browser test is what proves it is reachable at all:
phase 8 shipped a dispute panel that was fully built, fully tested at the
endpoint, and imported by nothing, so the entire feature was unreachable while
every test passed.
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


def _signup_owner(page, base_url: str) -> str:
    email = f"owner-{uuid.uuid4().hex[:10]}@example.com"
    page.goto(f"{base_url}/signup?role=owner")
    page.fill("#full_name", "Click Through")
    page.fill("#email", email)
    page.fill("#password", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url("**/dashboard")
    return email


def _add_home(page, base_url: str, nickname: str = "Maple Street") -> None:
    page.goto(f"{base_url}/properties/new")
    page.fill("#nickname", nickname)
    page.fill("#address_line1", "9 Maple St")
    page.fill("#city", "Portland")
    page.fill("#state", "ME")
    page.fill("#postal_code", "04101")
    # The radio is visually hidden behind its label, so click the label.
    page.get_by_text("A home", exact=True).click()
    page.click("button[type=submit]")
    page.wait_for_url(PROPERTY_DETAIL)
    expect(page.get_by_role("heading", name=nickname)).to_be_visible()


def test_an_owner_with_no_calendar_adds_a_season_of_cleans(page, live_server) -> None:
    _signup_owner(page, live_server)
    _add_home(page, live_server)

    # Reachable from the jobs list, where an owner with no feed actually is —
    # not tucked behind a calendar panel they will never open.
    page.goto(f"{live_server}/turnovers")
    page.get_by_test_id("bulk-link").click()
    page.wait_for_url("**/turnovers/bulk")

    page.select_option("#property", label="Maple Street")
    page.fill("#pasted", "2027-03-04\n2027-03-11\n2027-03-18 14:00")
    page.get_by_role("button", name="Add these dates").click()

    rows = page.get_by_test_id("bulk-row")
    expect(rows).to_have_count(3)

    # A home has no next guest, so the screen offers no box for one — the
    # server refuses a checkin there as a category error, and a form that
    # offered the field would be a form that lies.
    expect(page.get_by_text("Next checkin", exact=False)).to_have_count(0)

    page.select_option("#scope", label="Deep clean")
    page.fill("#budget", "160")
    page.get_by_role("button", name=re.compile("Add 3 as drafts")).click()

    result = page.get_by_test_id("bulk-result")
    expect(result).to_be_visible()
    expect(page.get_by_role("heading", name="3 drafts added")).to_be_visible()

    # **The screen says the thing the design turns on.** Drafts tell nobody,
    # and an owner who does not know that will wonder why no bids arrived.
    expect(page.get_by_text("they are drafts", exact=False)).to_be_visible()

    page.get_by_role("link", name="Go to jobs").click()
    page.wait_for_url("**/turnovers")
    expect(page.get_by_test_id("status-badge").first).to_be_visible()


def test_pasting_the_same_dates_twice_says_so_rather_than_duplicating(
    page, live_server
) -> None:
    """A duplicate is not refused — the owner asked for a job that is already
    there — but dropping it silently is how somebody pastes twice and books two
    cleaners for one clean."""
    _signup_owner(page, live_server)
    _add_home(page, live_server, nickname="Birch Lane")

    def add_them():
        page.goto(f"{live_server}/turnovers/bulk")
        page.select_option("#property", label="Birch Lane")
        page.fill("#pasted", "2027-05-06\n2027-05-13")
        page.get_by_role("button", name="Add these dates").click()
        expect(page.get_by_test_id("bulk-row")).to_have_count(2)
        page.get_by_role("button", name=re.compile("Add 2 as drafts")).click()
        expect(page.get_by_test_id("bulk-result")).to_be_visible()

    add_them()
    expect(page.get_by_role("heading", name="2 drafts added")).to_be_visible()

    add_them()
    expect(page.get_by_role("heading", name="0 drafts added")).to_be_visible()
    expect(page.get_by_text("already has a job then", exact=False)).to_be_visible()


def test_a_line_that_cannot_be_read_is_named_not_dropped(page, live_server) -> None:
    """A line that vanishes from a paste of forty is a job the owner thinks
    they scheduled."""
    _signup_owner(page, live_server)
    _add_home(page, live_server, nickname="Cedar Court")

    page.goto(f"{live_server}/turnovers/bulk")
    page.select_option("#property", label="Cedar Court")
    page.fill("#pasted", "2027-06-03\nnext tuesday\n2027-06-17")
    page.get_by_role("button", name="Add these dates").click()

    expect(page.get_by_test_id("bulk-row")).to_have_count(2)
    expect(page.get_by_text("could not be read", exact=False)).to_be_visible()
    expect(page.get_by_text("next tuesday", exact=False)).to_be_visible()


def _ics_bytes(*days_out: int) -> bytes:
    """A calendar file in the shape a listing site exports."""
    from datetime import date, timedelta

    events = []
    for index, offset in enumerate(days_out):
        start = date.today() + timedelta(days=offset)
        end = start + timedelta(days=3)
        events.append(
            "BEGIN:VEVENT\n"
            f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}\n"
            f"DTEND;VALUE=DATE:{end.strftime('%Y%m%d')}\n"
            f"UID:stay-{index}\n"
            "SUMMARY:Reserved\n"
            "END:VEVENT"
        )
    body = "\n".join(events)
    return (
        "BEGIN:VCALENDAR\n"
        "VERSION:2.0\n"
        "PRODID:-//Airbnb Inc//Hosting Calendar 1.0.0//EN\n"
        f"{body}\n"
        "END:VCALENDAR\n"
    ).encode()


def _add_rental(page, base_url: str, nickname: str) -> None:
    page.goto(f"{base_url}/properties/new")
    page.fill("#nickname", nickname)
    page.fill("#address_line1", "1 Harbor Way")
    page.fill("#city", "Portland")
    page.fill("#state", "ME")
    page.fill("#postal_code", "04101")
    page.click("button[type=submit]")
    page.wait_for_url(PROPERTY_DETAIL)
    expect(page.get_by_role("heading", name=nickname)).to_be_visible()


def test_uploading_a_calendar_file_fills_in_the_rows(page, live_server) -> None:
    """**The path that was shipped broken and no test clicked.**

    `apiFetch` stringified any body that was not undefined, and
    `JSON.stringify(new FormData())` is the string "{}" — so this did not throw,
    it posted an empty object and the server answered 422 about the file that
    had in fact been chosen. Every endpoint test passed, because they post
    multipart directly; only a browser doing what a person does goes through
    the helper.
    """
    _signup_owner(page, live_server)
    _add_rental(page, live_server, "Harbourside")

    page.goto(f"{live_server}/turnovers/bulk")
    page.select_option("#property", label="Harbourside")
    page.get_by_test_id("ics-upload").set_input_files(
        files=[
            {
                "name": "bookings.ics",
                "mimeType": "text/calendar",
                "buffer": _ics_bytes(6, 20),
            }
        ]
    )

    rows = page.get_by_test_id("bulk-row")
    expect(rows).to_have_count(2)
    # A rental does have a next guest, so the field is offered here.
    expect(page.get_by_text("Next checkin", exact=False).first).to_be_visible()

    page.get_by_role("button", name=re.compile("Add 2 as drafts")).click()
    expect(page.get_by_role("heading", name="2 drafts added")).to_be_visible()


def test_a_budget_that_cannot_be_read_is_refused_not_dropped(page, live_server) -> None:
    """`dollarsToCents` answers null for "abc" the same as for an empty box, so
    submitting it as "no budget" would create every draft without the number
    the owner typed. The single-job form corrects them; this has to as well."""
    _signup_owner(page, live_server)
    _add_home(page, live_server, nickname="Willow Way")

    page.goto(f"{live_server}/turnovers/bulk")
    page.select_option("#property", label="Willow Way")
    page.fill("#pasted", "2027-08-05")
    page.get_by_role("button", name="Add these dates").click()
    page.fill("#budget", "one hundred")
    page.get_by_role("button", name=re.compile("Add 1 as drafts")).click()

    expect(page.get_by_text("dollars and cents", exact=False)).to_be_visible()
    # Nothing was created, so the owner can correct it rather than hunting for
    # a dozen drafts with the budget missing.
    expect(page.get_by_test_id("bulk-result")).to_have_count(0)

    page.fill("#budget", "125.50")
    page.get_by_role("button", name=re.compile("Add 1 as drafts")).click()
    expect(page.get_by_role("heading", name="1 draft added")).to_be_visible()


def test_switching_property_clears_rows_rather_than_carrying_them(
    page, live_server
) -> None:
    """A rental's rows can carry checkins, which a home refuses as a category
    error — and the screen stops showing the field on a home, so kept rows
    would fail the whole batch over a value nobody can see or repair."""
    _signup_owner(page, live_server)
    _add_rental(page, live_server, "Dockside")
    _add_home(page, live_server, nickname="Elm House")

    page.goto(f"{live_server}/turnovers/bulk")
    page.select_option("#property", label="Dockside")
    page.fill("#pasted", "2027-09-02\n2027-09-09")
    page.get_by_role("button", name="Add these dates").click()
    expect(page.get_by_test_id("bulk-row")).to_have_count(2)

    page.select_option("#property", label="Elm House")
    expect(page.get_by_test_id("bulk-row")).to_have_count(0)
    # Said out loud, because silently losing typed work is its own bug.
    expect(page.get_by_text("were cleared", exact=False)).to_be_visible()


def test_every_draft_created_is_reachable_from_the_list(page, live_server) -> None:
    """**A job the list cannot reach is a job nobody can post, edit or cancel.**

    `/turnovers` has always taken `limit` and `offset`; the screen asked for
    neither and so showed the API's default fifty with no way past them. That
    was survivable while jobs arrived one form at a time, and stopped being so
    the moment this feature could add a hundred at once — the drafts past the
    fiftieth existed and could not be opened.
    """
    from datetime import date, timedelta

    _signup_owner(page, live_server)
    _add_home(page, live_server, nickname="Long List")

    # 60 dates: more than one page, well inside MAX_BULK_JOBS.
    start = date.today() + timedelta(days=30)
    dates = "\n".join(
        (start + timedelta(days=offset)).strftime("%Y-%m-%d") for offset in range(60)
    )

    page.goto(f"{live_server}/turnovers/bulk")
    page.select_option("#property", label="Long List")
    page.fill("#pasted", dates)
    page.get_by_role("button", name="Add these dates").click()
    expect(page.get_by_test_id("bulk-row")).to_have_count(60)
    page.get_by_role("button", name=re.compile("Add 60 as drafts")).click()
    expect(page.get_by_role("heading", name="60 drafts added")).to_be_visible()

    page.get_by_role("link", name="Go to jobs").click()
    page.wait_for_url("**/turnovers")

    cards = page.get_by_test_id("status-badge")
    expect(cards).to_have_count(50)
    page.get_by_test_id("load-more").click()
    expect(cards).to_have_count(60)
    # Nothing left to ask for, so the button goes.
    expect(page.get_by_test_id("load-more")).to_have_count(0)


def test_an_upload_that_lands_after_a_switch_is_discarded(page, live_server) -> None:
    """Parsing is a round trip, and the owner can change the property while it
    is in flight. Appending the answer then would put one property's bookings
    on another's list — silently, and after the switch handler had already
    cleared the rows for exactly that reason."""
    _signup_owner(page, live_server)
    _add_rental(page, live_server, "Pier View")
    _add_home(page, live_server, nickname="Oak Cottage")

    page.goto(f"{live_server}/turnovers/bulk")

    # Hold the parse open, so the switch lands first — the real ordering, not a
    # simulated one.
    page.route(
        "**/calendars/read-file",
        lambda route: (page.wait_for_timeout(1200), route.continue_()),
    )

    page.select_option("#property", label="Pier View")
    page.get_by_test_id("ics-upload").set_input_files(
        files=[
            {
                "name": "bookings.ics",
                "mimeType": "text/calendar",
                "buffer": _ics_bytes(6, 20),
            }
        ]
    )
    page.select_option("#property", label="Oak Cottage")

    # The answer for Pier View arrives while Oak Cottage is selected.
    page.wait_for_timeout(2000)
    expect(page.get_by_test_id("bulk-row")).to_have_count(0)
