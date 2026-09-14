"""Bid → award → the job done → the owner pays, clicked through in a browser.

**This is the carry-over row finished.** The list has asked since day one for
"a real click-through of bid → award → payment, not just 'the button renders'",
and phase 4 could only deliver the first two thirds of it. This is the rest.

What only a browser can check here is the handovers — the places where one
screen has to become another without anybody's state going blank:

* the cleaner marks the job done and their own card has to *become* the
  finished state rather than emptying, because the action answers with the whole
  job shape;
* the owner's turnover page has to grow a pay panel showing the split, which is
  the first time the owner sees what the platform keeps;
* clicking pay has to actually leave for a hosted page and come back, with the
  webhook landing in between — the redirect, the signature and the settlement
  are three separate things that all have to work for the screen to say "Paid";
* and the cleaner's payout setup has to be a real round trip out to Stripe and
  back, ending with the *account* saying payouts are enabled rather than the app
  assuming so because somebody came back from a redirect.

Stripe is a real HTTP server on another port (`fake_stripe.py`), not a patched
function, so the app's own client, form encoding, redirect handling and webhook
signature verification all run for real. Only the far end is a fake, and it is
honest about that: nothing here replaces walking one payment through a real
test-mode key before launch.
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
    """Finish vetting the way an admin would. `can_take_jobs` is generated, so
    the statuses go through the same door the admin's buttons use."""
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


def test_an_owner_pays_for_a_finished_job(make_page, live_server, db, fake_stripe) -> None:
    base_url = live_server
    owner_page, cleaner_page = make_page(), make_page()

    # --- the owner posts a job ---
    page = owner_page
    _signup(page, base_url, "owner", "Pat Owner")

    page.goto(f"{base_url}/properties/new")
    page.fill("#nickname", "Harbor Loft")
    page.fill("#address_line1", "9 Commercial St")
    page.fill("#city", "Portland")
    page.fill("#state", "ME")
    page.fill("#postal_code", "04101")
    page.click("button[type=submit]")
    page.wait_for_url(re.compile(r"/properties/[0-9a-fA-F-]{36}$"))
    _place_coordinates(db, "Harbor Loft")

    page.goto(f"{base_url}/turnovers/new")
    page.fill("#checkout_at", "2027-06-10T11:00")
    page.fill("#checkin_at", "2027-06-10T16:00")
    page.fill("#budget", "220")
    page.click("button[type=submit]")
    page.wait_for_url(re.compile(r"/turnovers/[0-9a-fA-F-]{36}$"))
    turnover_url = page.url

    # --- a cleaner signs up, is cleared, and sets up payouts ---
    page = cleaner_page
    page.expected_errors.append("404 (Not Found)")  # first visit, no profile yet
    cleaner_email = _signup(page, base_url, "cleaner", "Robin Vale")

    page.goto(f"{base_url}/cleaner/profile")
    _set_service_area(page)
    page.click("button[type=submit]")
    expect(page.get_by_role("heading", name="Not cleared to bid yet")).to_be_visible()
    _clear_for_work(db, cleaner_email)

    # Payout setup is a separate panel from vetting on purpose: they answer
    # different questions, and a cleaner can be cleared to bid while still
    # unable to be paid.
    page.reload()
    expect(page.get_by_test_id("payout-heading")).to_have_text("Not set up to get paid yet")
    page.get_by_test_id("payout-setup").click()

    # Out to Stripe's hosted onboarding, and back.
    page.wait_for_url(re.compile(r"/onboarding$"))
    page.click("#done")
    page.wait_for_url(re.compile(r"/cleaner/profile"))
    expect(page.get_by_test_id("payout-heading")).to_have_text("Set up to get paid")

    # --- the cleaner bids, the owner accepts ---
    page.goto(f"{base_url}/board")
    expect(page.get_by_role("button", name="Place bid")).to_be_visible()
    page.fill("input[id^=price-]", "200")
    page.get_by_role("button", name="Place bid").click()
    expect(page.get_by_text(re.compile(r"Your bid:\s*\$200"))).to_be_visible()

    page = owner_page
    page.goto(turnover_url)
    page.get_by_test_id("accept-bid").click()
    expect(page.get_by_test_id("award-panel")).to_be_visible()
    # Nothing to pay yet — the job has not been done.
    expect(page.get_by_test_id("payment-panel")).to_have_count(0)

    # --- the cleaner does the job and marks it done ---
    page = cleaner_page
    page.goto(f"{base_url}/jobs")
    expect(page.get_by_test_id("job")).to_have_count(1)
    page.get_by_test_id("start-job").click()
    page.get_by_test_id("complete-job").click()

    # The card has to *become* the finished state, not empty out. The action
    # answers with the whole job shape for exactly this reason.
    expect(page.get_by_test_id("job-done")).to_be_visible()
    expect(page.get_by_test_id("job-address")).to_contain_text("9 Commercial St")
    expect(page.get_by_test_id("complete-job")).to_have_count(0)

    # --- the owner pays ---
    page = owner_page
    page.goto(turnover_url)
    expect(page.get_by_test_id("payment-heading")).to_have_text("Ready to pay")

    page.get_by_test_id("pay-button").click()

    # Out to the hosted page. The card never touches linx.
    page.wait_for_url(re.compile(r"/pay/cs_e2e_"))
    expect(page.get_by_text("Amount: 20000")).to_be_visible()
    page.click("#pay")

    # Back on the turnover, settled — which took a real signed webhook landing
    # while the browser was being redirected.
    page.wait_for_url(re.compile(r"/turnovers/[0-9a-fA-F-]{36}"))
    expect(page.get_by_test_id("payment-heading")).to_have_text("Paid")
    expect(page.get_by_test_id("payment-total")).to_have_text("$200")
    expect(page.get_by_test_id("payment-cleaner")).to_have_text("$170")
    # The page it came back to is still the whole turnover, not a stub.
    expect(page.get_by_role("heading", name="Harbor Loft")).to_be_visible()

    # --- and the charge that went over the wire was the right shape ---
    sent = fake_stripe.body_for("/v1/checkout/sessions")
    intent = sent["payment_intent_data"]
    assert intent["transfer_data"]["destination"] == "acct_e2e", (
        "not a destination charge — the cleaner's money would stay on the platform"
    )
    assert intent["application_fee_amount"] == "3000"
    assert sent["line_items"]["0"]["price_data"]["unit_amount"] == "20000"

    # --- the books balance ---
    from sqlalchemy import select

    from app.models.payment import PaymentIn, Payout
    from app.services import payments

    db.expire_all()
    payment = db.execute(select(PaymentIn)).scalars().one()
    payout = db.execute(select(Payout)).scalars().one()
    assert payments.reconcile(payment, payout)["drift_cents"] == 0
