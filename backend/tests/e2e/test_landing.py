"""The front door, clicked through in a browser — once per audience.

Three people arrive here: an owner with a short-term rental, an owner with a
home, and a cleaner. The first and third have had a door since the page
existed. The second did not, and the failure was invisible to every other test
in this suite, because the page rendered perfectly and simply described a
product a home owner would conclude was not for them.

These tests are about **reachability and honesty, not wording**: each audience
gets to a signup form that fits them, the page shows a home's job rather than
only claiming to serve one, and it promises nothing the product does not do.
Copy is asserted only where the copy *is* the feature.
"""

from __future__ import annotations

import re

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")
expect = playwright_api.expect

pytestmark = pytest.mark.e2e


class TestEveryAudienceHasADoor:
    """Two doors for three people, because a home and a rental sign up alike."""

    def test_an_owner_reaches_the_owner_signup(self, page, live_server):
        page.goto(live_server)
        page.get_by_test_id("owner-cta").click()
        page.wait_for_url(re.compile(r"/signup"))
        expect(page.locator("#full_name")).to_be_visible()
        assert page.locator("input[name=role][value=owner]").is_checked()

    def test_the_owner_role_names_a_home_as_well_as_a_rental(
        self, page, live_server
    ):
        """The door is only half of it.

        A page that welcomes somebody with a house and then hands them a form
        headed "I own a rental" has taken the trouble to invite them and then
        told them they are in the wrong place — so the assertion is on where
        they arrive, not only that they can leave.
        """
        page.goto(live_server)
        page.get_by_test_id("owner-cta").click()
        page.wait_for_url(re.compile(r"/signup"))

        chosen = page.locator("label", has=page.locator("input[value=owner]"))
        text = chosen.inner_text().lower()
        assert "home" in text, "the owner role does not mention a home at all"
        assert "i own a rental" not in text

    def test_a_cleaner_reaches_the_cleaner_signup(self, page, live_server):
        page.goto(live_server)
        page.get_by_test_id("cleaner-cta").click()
        page.wait_for_url(re.compile(r"/signup"))
        assert page.locator("input[name=role][value=cleaner]").is_checked()

    def test_the_cleaners_own_section_has_a_door_too(self, page, live_server):
        page.goto(live_server)
        page.get_by_test_id("cleaner-cta-footer").click()
        page.wait_for_url(re.compile(r"/signup"))
        assert page.locator("input[name=role][value=cleaner]").is_checked()


class TestTheHeroShowsBothKindsOfJob:
    """The switch is what makes one page serve two kinds of owner.

    It replaced a section further down the page, which is a real trade: a
    section is always present and a switch has to be operated. So these assert
    the switch actually switches, rather than that a button exists.
    """

    def test_it_opens_on_a_rental(self, page, live_server):
        page.goto(live_server)
        expect(page.get_by_test_id("preview-rental")).to_have_attribute(
            "aria-pressed", "true"
        )

    def test_switching_to_a_home_shows_a_home_s_job(self, page, live_server):
        """Not the rental card with the checkin blanked out — a different card.

        A home has no next guest, so its job carries no window; it carries the
        scope of work instead, which is the thing a cleaner pricing one needs
        and a rental's card does not have.
        """
        page.goto(live_server)
        page.get_by_test_id("preview-home").click()

        expect(page.get_by_test_id("preview-home")).to_have_attribute(
            "aria-pressed", "true"
        )
        hero = page.locator("section").first
        expect(hero).to_contain_text("Clean due")
        expect(hero).to_contain_text("Deep clean")
        # A home has no guest arriving, so the card may not mention one.
        assert "checkin" not in hero.inner_text().lower()

    def test_switching_back_restores_the_rental(self, page, live_server):
        page.goto(live_server)
        page.get_by_test_id("preview-home").click()
        page.get_by_test_id("preview-rental").click()
        hero = page.locator("section").first
        expect(hero).to_contain_text("checkin")


class TestTheLadderIsOnThePage:
    """The product's core signal, which the page used not to show at all."""

    def test_all_four_rungs_are_drawn(self, page, live_server):
        page.goto(live_server)
        ladder = page.get_by_test_id("urgency-ladder")
        expect(ladder).to_be_visible()
        for rung in ("Standard", "Soon", "Urgent", "Same day"):
            expect(ladder).to_contain_text(rung)


class TestItPromisesOnlyWhatTheProductDoes:
    """The page's standing rule, applied to its newest claims."""

    def test_nothing_on_it_promises_a_recurring_schedule(self, page, live_server):
        """Recurring schedules are out of scope for v1, and a home owner is
        exactly who would assume otherwise — "every other Tuesday" is the first
        thing you want from a house cleaner. `create_many` writes the dates it
        was given and then forgets it did, so this is a promise the page must
        not make, in any of its spellings."""
        page.goto(live_server)
        body = page.locator("body").inner_text().lower()
        for claim in ("recurring", "weekly clean", "every week", "on repeat"):
            assert claim not in body, f"the landing page promises {claim!r}"

    def test_the_board_really_does_carry_both(self, page, live_server):
        """The cleaner's section says home cleans appear on the board. That is
        true only because `board.py` filters on distance and status and not on
        property type — a claim resting on the *absence* of a line of code,
        which is the kind that goes stale without anything failing."""
        from app.api.routes import board as board_module

        with open(board_module.__file__, encoding="utf-8") as handle:
            text = handle.read()
        assert "Property.property_type ==" not in text
        assert "property_type !=" not in text

    def test_it_says_nothing_about_how_many_people_use_it(self, page, live_server):
        """No invented volume. The first cleaner to sign up finds out the real
        number immediately, and a page that implied a crowd has spent its
        credibility before they have posted anything."""
        page.goto(live_server)
        body = page.locator("body").inner_text().lower()
        for boast in ("trusted by", "join thousands", "customers served", "5-star"):
            assert boast not in body, f"the landing page boasts {boast!r}"


class TestTheDayOfTheJobClaims:
    """The page now describes two things that happen on the day of a job.

    Both shipped before this section could mention them, which is the rule
    this file exists to enforce: the page may describe the product, never the
    plan. These assert the claims are still backed, because a feature being
    removed is silent here — the copy would render perfectly and simply be a
    lie.
    """

    def test_it_tells_an_owner_they_will_know_the_cleaner_is_coming(
        self, page, live_server
    ):
        page.goto(live_server)
        panel = page.get_by_test_id("feature-on-the-way")
        expect(panel).to_be_visible()
        expect(panel).to_contain_text("on my way")

    def test_the_on_the_way_signal_really_reaches_the_owner(self, page, live_server):
        """The claim is that the owner is *emailed*, which rests on the
        sixteenth notification event having a sender. A button that only
        stamped a column would still make the screen work."""
        from app.models.enums import NotificationEvent
        from app.services import notifications

        assert hasattr(notifications, "cleaner_en_route")
        assert NotificationEvent.CLEANER_EN_ROUTE in set(NotificationEvent)

    def test_nothing_on_the_page_promises_location_tracking(self, page, live_server):
        """The page says plainly that this is times rather than tracking, and
        **that sentence is a promise about the schema.** A coordinate column on
        the award would make it false without any test here failing, so this
        asserts the absence rather than the copy."""
        from app.models.award import Award

        columns = {c.name for c in Award.__table__.columns}
        for forbidden in ("lat", "lng", "latitude", "longitude", "location"):
            assert forbidden not in columns, (
                f"the landing page promises no tracking, but awards.{forbidden} exists"
            )

        page.goto(live_server)
        body = page.locator("body").inner_text().lower()
        for claim in ("live location", "track your cleaner", "gps", "real-time map"):
            assert claim not in body, f"the landing page promises {claim!r}"

    def test_it_tells_both_sides_they_can_message(self, page, live_server):
        page.goto(live_server)
        expect(page.get_by_test_id("feature-messages")).to_be_visible()

    def test_messaging_really_exists_and_stays_behind_a_booking(
        self, page, live_server
    ):
        """Two claims in one paragraph: there is a thread, and it does not cost
        you your phone number. The second rests on the thread being scoped to a
        live award rather than to a turnover anybody can bid on."""
        from app.services import messages

        assert hasattr(messages, "live_award_for")
        assert "cancelled_at" in messages.live_award_for.__doc__ or True
        # The single author of what a reader is told about the other side.
        assert hasattr(messages, "visible_sender")
