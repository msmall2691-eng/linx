"""The front door, clicked through in a browser — once per audience.

There are three people this page has to work for: an owner with a short-term
rental, an owner with a home, and a cleaner. The first and the third have had
a door of their own since the page existed. The second did not, and the failure
was invisible to every test in this suite, because the page rendered perfectly
and simply described a product a home owner would conclude was not for them.

So these tests are about **reachability, not wording**: each audience gets from
the front page to a signup form that has not just told them they took a wrong
turn. Copy is asserted only where the copy is the feature — the signpost, and
the one claim about repeating that the product deliberately does not support.
"""

from __future__ import annotations

import re

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")
expect = playwright_api.expect

pytestmark = pytest.mark.e2e


class TestEveryAudienceHasADoor:
    """Three doors, and each one lands somewhere that fits the person."""

    def test_a_rental_owner_reaches_the_owner_signup(self, page, live_server):
        page.goto(live_server)
        page.get_by_test_id("owner-cta").click()
        page.wait_for_url(re.compile(r"/signup"))
        expect(page.locator("#full_name")).to_be_visible()
        assert page.locator("input[name=role][value=owner]").is_checked()

    def test_a_home_owner_reaches_the_same_signup_and_is_not_told_otherwise(
        self, page, live_server
    ):
        """The journey this page was missing, and it crosses two screens.

        The door is only half of it. A button reading "I own a home" that
        lands on a form headed "I own a rental" has taken the trouble to
        welcome somebody and then told them they are in the wrong place — so
        the assertion is on where they arrive, not only that they can leave.
        """
        page.goto(live_server)
        page.get_by_test_id("home-cta").click()
        page.wait_for_url(re.compile(r"/signup"))
        assert page.locator("input[name=role][value=owner]").is_checked()

        # The role they just chose must name them rather than exclude them.
        chosen = page.locator("label", has=page.locator("input[value=owner]"))
        text = chosen.inner_text().lower()
        assert "home" in text, "the owner role does not mention a home at all"
        assert "i own a rental" not in text

    def test_a_cleaner_still_reaches_the_cleaner_signup(self, page, live_server):
        """A regression guard: the cleaner's section was edited for this too."""
        page.goto(live_server)
        page.get_by_test_id("cleaner-cta").click()
        page.wait_for_url(re.compile(r"/signup"))
        assert page.locator("input[name=role][value=cleaner]").is_checked()

    def test_the_closing_row_offers_all_three(self, page, live_server):
        page.goto(live_server)
        page.get_by_test_id("home-owner-cta").click()
        page.wait_for_url(re.compile(r"/signup"))
        assert page.locator("input[name=role][value=owner]").is_checked()


class TestNobodyLeavesBeforeTheirSection:
    """The hero is about guests arriving at 4, which is not a home's problem."""

    def test_the_signpost_reaches_the_home_section(self, page, live_server):
        """A `#homes` link with no `id="homes"` lints clean, builds clean, and
        silently does nothing — which is exactly the failure the signpost
        exists to prevent, arriving by a different route."""
        page.goto(live_server)
        signpost = page.get_by_test_id("homes-signpost")
        expect(signpost).to_be_visible()
        assert signpost.get_attribute("href") == "#homes"
        expect(page.locator("#homes")).to_have_count(1)

    def test_the_home_section_carries_its_own_preview(self, page, live_server):
        """Its own screen, like the cleaner's — not the rental card with the
        checkin blanked out, which is what a home's job actually is not."""
        page.goto(live_server)
        homes = page.locator("#homes")
        expect(homes).to_contain_text("Clean due")
        # A home has no next guest, so its preview may not show one.
        assert "checkin" not in homes.inner_text().lower()


class TestItPromisesOnlyWhatTheProductDoes:
    """The page's own standing rule, applied to the newest claims on it."""

    def test_nothing_on_it_promises_a_recurring_schedule(self, page, live_server):
        """Recurring schedules are out of scope for v1, and a home owner is
        exactly who would assume otherwise. `create_many` writes the dates it
        was given and then forgets it did — so "every other Tuesday" is a
        promise this page must not make, in any of its spellings."""
        page.goto(live_server)
        body = page.locator("body").inner_text().lower()
        for claim in ("recurring", "weekly clean", "every week", "on repeat"):
            assert claim not in body, f"the landing page promises {claim!r}"
        # And says so plainly rather than merely staying quiet.
        expect(page.locator("#homes")).to_contain_text("Nothing repeats on its own")

    def test_the_board_really_does_carry_both(self, page, live_server):
        """The cleaner's section now says home cleans appear on the board. That
        is only true because `board.py` filters on distance and status and not
        on property type — a claim resting on the *absence* of a line of code,
        which is the kind that goes stale without anything failing."""
        from app.api.routes import board as board_module

        source = board_module.__file__
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
        assert "Property.property_type ==" not in text
        assert "property_type !=" not in text
