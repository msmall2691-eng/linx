"""The money path — guardrail 2, and the three carry-over rows it owed.

The carry-over list in CLAUDE.md has had these written down since day one:

* *Stripe idempotency — replay the same charge attempt, assert no duplicate.*
* *Collected-vs-paid-out reconciliation — collected always equals payout plus
  platform fee, no drift.*
* *Refund path — fee and transfer both resolve, nothing left stranded.*

All three are here, and each is written to fail against the wrong
implementation rather than merely to pass against the right one. The
idempotency test asserts the **same derived key** on both attempts, which is
the whole mechanism: a `uuid4()` per attempt would satisfy "a key was sent" and
still bill the customer twice.

Stripe itself is replaced by a recording fake at the transport boundary
(`stripe_client.post` / `.get`), so every request shape this codebase sends is
visible and asserted. No request has ever been made to a real Stripe account
from this repository — the fake is honest about being a fake, and walking one
payment through a test-mode key before launch is still on the list.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    Award,
    CleanerProfile,
    NotificationEvent,
    PaymentStatus,
    Turnover,
    TurnoverStatus,
)
from app.models.payment import PaymentIn, Payout
from app.services import notifications, payments, stripe_client


# --------------------------------------------------------------------------
# A fake Stripe that records everything it was asked to do
# --------------------------------------------------------------------------


class FakeStripe:
    """Stands in for the network, and remembers every call.

    Deliberately records the idempotency key alongside the body: the tests
    below are about *what was sent*, not only about what came back.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.responses: dict[str, dict] = {}
        self.failures: dict[str, Exception] = {}

    def post(
        self,
        path: str,
        data: dict,
        *,
        idempotency_key: str | None = None,
        non_idempotent_reason: str | None = None,
        stripe_account: str | None = None,
    ) -> dict:
        if not idempotency_key and not non_idempotent_reason:
            raise ValueError("a mutating Stripe call needs a key or a signed reason")
        self.calls.append(
            {
                "path": path,
                "data": data,
                "idempotency_key": idempotency_key,
                "non_idempotent_reason": non_idempotent_reason,
            }
        )
        if path in self.failures:
            raise self.failures[path]
        return self.responses.get(path, {"id": f"fake_{len(self.calls)}"})

    def get(self, path: str, *, stripe_account: str | None = None) -> dict:
        self.calls.append({"path": path, "data": None, "idempotency_key": None})
        if path in self.failures:
            raise self.failures[path]
        return self.responses.get(path, {"id": path.rsplit("/", 1)[-1]})

    def paths(self, path: str) -> list[dict]:
        return [call for call in self.calls if call["path"] == path]


@pytest.fixture
def stripe(monkeypatch) -> FakeStripe:
    fake = FakeStripe()
    monkeypatch.setattr(stripe_client, "post", fake.post)
    monkeypatch.setattr(stripe_client, "get", fake.get)
    monkeypatch.setattr(stripe_client, "is_configured", lambda: True)
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_fake")
    fake.responses["/accounts"] = {"id": "acct_fake"}
    fake.responses["/account_links"] = {"url": "https://connect.stripe.test/setup"}
    fake.responses["/checkout/sessions"] = {
        "id": "cs_test_fake",
        "url": "https://checkout.stripe.test/pay/cs_test_fake",
        "payment_intent": "pi_test_fake",
    }
    fake.responses["/refunds"] = {"id": "re_test_fake", "status": "succeeded"}
    return fake


# --------------------------------------------------------------------------
# Helpers — a job taken all the way to "done and payable"
# --------------------------------------------------------------------------


def _bid(client: TestClient, cleaner: dict, turnover_id: str, cents: int = 20_000) -> dict:
    resp = client.put(
        f"/api/board/{turnover_id}/bid",
        json={"price_cents": cents, "message": "On it."},
        headers=cleaner["auth"],
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _payout_ready(db: Session, cleaner: dict) -> CleanerProfile:
    profile = db.execute(
        select(CleanerProfile).where(
            CleanerProfile.user_id == uuid.UUID(cleaner["user"]["id"])
        )
    ).scalar_one()
    profile.stripe_account_id = f"acct_{profile.id.hex[:12]}"
    profile.stripe_details_submitted = True
    profile.stripe_payouts_enabled = True
    db.commit()
    return profile


def _completed_job(
    client: TestClient,
    make_cleaner,
    make_open_turnover,
    db: Session,
    *,
    price_cents: int = 20_000,
    payout_ready: bool = True,
) -> dict:
    """Bid → award → started → complete. The state money is raised against."""
    job = make_open_turnover()
    cleaner = make_cleaner(cleared=True)
    if payout_ready:
        _payout_ready(db, cleaner)

    bid = _bid(client, cleaner, job["turnover"]["id"], price_cents)
    accepted = client.post(
        f"/api/turnovers/{job['turnover']['id']}/bids/{bid['id']}/accept",
        headers=job["owner"]["auth"],
    )
    assert accepted.status_code == 200, accepted.text

    done = client.post(
        f"/api/board/jobs/{job['turnover']['id']}/complete", headers=cleaner["auth"]
    )
    assert done.status_code == 200, done.text

    job["cleaner"] = cleaner
    return job


def _payment(db: Session, turnover_id: str) -> PaymentIn:
    db.expire_all()
    return db.execute(
        select(PaymentIn).where(PaymentIn.turnover_id == uuid.UUID(turnover_id))
    ).scalar_one()


# --------------------------------------------------------------------------
# The split — integer cents, and the two halves that must add back up
# --------------------------------------------------------------------------


class TestTheSplit:
    def test_the_fee_and_the_cleaner_add_back_to_the_agreed_price(self) -> None:
        split = payments.split_for(20_000, fee_bps=1500)
        assert split.platform_fee_cents == 3_000
        assert split.cleaner_cents == 17_000
        assert split.platform_fee_cents + split.cleaner_cents == split.total_cents

    @pytest.mark.parametrize("cents", [1, 7, 99, 101, 333, 12_345, 999_999])
    def test_they_always_add_back_up_whatever_the_price(self, cents: int) -> None:
        """The property that matters, not one worked example.

        The cleaner's share is the remainder rather than its own percentage,
        because two independent roundings are how a cent goes missing.
        """
        split = payments.split_for(cents)
        assert split.platform_fee_cents + split.cleaner_cents == cents
        assert isinstance(split.platform_fee_cents, int)
        assert isinstance(split.cleaner_cents, int)

    def test_a_fraction_of_a_cent_goes_to_the_cleaner_not_to_us(self) -> None:
        # 15% of 333 is 49.95. Truncating the fee to 49 leaves 284 for the
        # cleaner; rounding it up to 50 would take the half-cent off a person's
        # pay. The direction is a decision, so it is asserted.
        split = payments.split_for(333, fee_bps=1500)
        assert split.platform_fee_cents == 49
        assert split.cleaner_cents == 284

    def test_there_is_nothing_to_charge_for_a_zero_price(self) -> None:
        with pytest.raises(payments.PaymentRefused):
            payments.split_for(0)


# --------------------------------------------------------------------------
# Completion — the transition money hangs off
# --------------------------------------------------------------------------


class TestMarkingTheJobDone:
    def test_completing_makes_it_payable_and_tells_the_owner(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        job = _completed_job(client, make_cleaner, make_open_turnover, db)

        turnover = db.get(Turnover, uuid.UUID(job["turnover"]["id"]))
        db.refresh(turnover)
        assert turnover.status is TurnoverStatus.COMPLETED

        award = db.execute(
            select(Award).where(Award.turnover_id == turnover.id)
        ).scalar_one()
        assert award.completed_at is not None
        assert award.started_at is not None, "finishing implies having started"

        told = {
            row.destination
            for row in db.execute(
                select(notifications.Notification).where(
                    notifications.Notification.event == NotificationEvent.JOB_COMPLETED
                )
            ).scalars()
        }
        assert job["owner"]["user"]["email"] in told, "the owner was never told to pay"

    def test_marking_it_done_twice_does_not_tell_the_owner_twice(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        """A double tap on a phone in a driveway is not an error."""
        job = _completed_job(client, make_cleaner, make_open_turnover, db)
        again = client.post(
            f"/api/board/jobs/{job['turnover']['id']}/complete",
            headers=job["cleaner"]["auth"],
        )
        assert again.status_code == 200

        rows = db.execute(
            select(notifications.Notification).where(
                notifications.Notification.event == NotificationEvent.JOB_COMPLETED
            )
        ).scalars().all()
        assert len(rows) == 1

    def test_somebody_elses_job_is_not_yours_to_finish(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        job = _completed_job(client, make_cleaner, make_open_turnover, db)
        stranger = make_cleaner(cleared=True)
        resp = client.post(
            f"/api/board/jobs/{job['turnover']['id']}/complete", headers=stranger["auth"]
        )
        # 404 rather than 403 — a 403 would confirm the id exists.
        assert resp.status_code == 404

    def test_a_cancelled_booking_cannot_be_completed(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        job = make_open_turnover()
        cleaner = make_cleaner(cleared=True)
        bid = _bid(client, cleaner, job["turnover"]["id"])
        client.post(
            f"/api/turnovers/{job['turnover']['id']}/bids/{bid['id']}/accept",
            headers=job["owner"]["auth"],
        )
        client.post(
            f"/api/board/jobs/{job['turnover']['id']}/cancel",
            json={"reason": "Cannot make it."},
            headers=cleaner["auth"],
        )
        resp = client.post(
            f"/api/board/jobs/{job['turnover']['id']}/complete", headers=cleaner["auth"]
        )
        assert resp.status_code == 404


class TestStartingWorkDoesNotSwitchOffThePolicy:
    """The seam phase 6 opened, and the tests that keep it shut.

    Before this phase `IN_PROGRESS` was unreachable, so "a live booking" and
    "status is AWARDED" were the same sentence and every guard on the
    cancellation path spelled the second one. The moment a cleaner could tap
    "I'm on site", that stopped being true — and every one of these paths would
    have failed *silently*, with a 409 saying "there is nobody booked".

    Which is a lie, and worse, it is the lie on the path CLAUDE.md describes as
    never conditional and never silent.
    """

    def _started_job(self, client, make_cleaner, make_open_turnover, db) -> dict:
        job = make_open_turnover()
        cleaner = make_cleaner(cleared=True)
        bid = _bid(client, cleaner, job["turnover"]["id"])
        client.post(
            f"/api/turnovers/{job['turnover']['id']}/bids/{bid['id']}/accept",
            headers=job["owner"]["auth"],
        )
        started = client.post(
            f"/api/board/jobs/{job['turnover']['id']}/start", headers=cleaner["auth"]
        )
        assert started.status_code == 200, started.text
        job["cleaner"] = cleaner
        return job

    def test_a_cleaner_who_started_can_still_back_out(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        """"Always allowed" is the whole policy, not a default.

        A cleaner who cannot say "I can't make it" says nothing instead, and the
        owner finds out by arriving at a dirty house.
        """
        job = self._started_job(client, make_cleaner, make_open_turnover, db)
        resp = client.post(
            f"/api/board/jobs/{job['turnover']['id']}/cancel",
            json={"reason": "Water main burst, I have to go."},
            headers=job["cleaner"]["auth"],
        )
        assert resp.status_code == 200, resp.text

        turnover = db.get(Turnover, uuid.UUID(job["turnover"]["id"]))
        db.refresh(turnover)
        assert turnover.status is TurnoverStatus.OPEN, "the job never went back on the bench"

    def test_tapping_start_does_not_make_a_no_show_unrecordable(
        self, client: TestClient, make_cleaner, make_open_turnover, admin_user, db: Session
    ) -> None:
        """The failure this guards against is somebody gaming it.

        Tap "I'm on site" from the driveway, drive away, and — if the no-show
        path keyed on AWARDED alone — the owner could no longer record it.
        `was_no_show` is the history a dispute is argued from.
        """
        job = self._started_job(client, make_cleaner, make_open_turnover, db)
        resp = client.post(
            f"/api/turnovers/{job['turnover']['id']}/no-show",
            json={"reason": "Marked themselves on site. Nobody came."},
            headers=job["owner"]["auth"],
        )
        assert resp.status_code == 200, resp.text

        award = db.execute(
            select(Award).where(Award.turnover_id == uuid.UUID(job["turnover"]["id"]))
        ).scalar_one()
        db.refresh(award)
        assert award.was_no_show is True

        told = {
            row.destination
            for row in db.execute(
                select(notifications.Notification).where(
                    notifications.Notification.event == NotificationEvent.CLEANER_NO_SHOW
                )
            ).scalars()
        }
        assert job["owner"]["user"]["email"] in told
        assert job["cleaner"]["user"]["email"] in told
        assert admin_user["user"].email in told

    def test_the_day_of_reminder_survives_an_early_start(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        """Nothing gates `start`, so it can be tapped days early.

        A reminder that stops firing is the quietest possible regression: no
        error, no failing request, just two people who are never reminded.
        """
        from datetime import timedelta

        from app.tasks import scheduled

        job = self._started_job(client, make_cleaner, make_open_turnover, db)
        turnover = db.get(Turnover, uuid.UUID(job["turnover"]["id"]))
        db.refresh(turnover)
        when = turnover.checkout_at - timedelta(hours=1)

        queued = scheduled.send_reminders(db, now=when)
        assert queued == 2, "both sides should still be reminded"

        told = {
            row.destination
            for row in db.execute(
                select(notifications.Notification).where(
                    notifications.Notification.event == NotificationEvent.TURNOVER_REMINDER
                )
            ).scalars()
        }
        assert job["owner"]["user"]["email"] in told
        assert job["cleaner"]["user"]["email"] in told

    def test_a_finished_job_is_a_dispute_not_a_no_show(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session
    ) -> None:
        """`COMPLETED` is deliberately outside the live-booking statuses.

        Once the work is done, "they did not turn up" is not the conversation —
        a refund is, and it goes through a human.
        """
        job = _completed_job(client, make_cleaner, make_open_turnover, db)
        resp = client.post(
            f"/api/turnovers/{job['turnover']['id']}/no-show",
            json={"reason": "Changed my mind about the clean."},
            headers=job["owner"]["auth"],
        )
        assert resp.status_code == 409

        cancel = client.post(
            f"/api/board/jobs/{job['turnover']['id']}/cancel",
            json={"reason": "Actually I want out."},
            headers=job["cleaner"]["auth"],
        )
        assert cancel.status_code == 409


# --------------------------------------------------------------------------
# Guardrail 2 — the carry-over idempotency row
# --------------------------------------------------------------------------


class TestIdempotency:
    def test_replaying_the_charge_sends_the_same_derived_key(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session, stripe
    ) -> None:
        """**The carry-over test.** Replay the attempt; assert no duplicate.

        The assertion that matters is not "two calls happened" — it is that both
        carried the *same* key, derived from the award id. A freshly generated
        key per attempt would pass a weaker test and bill the customer twice,
        which is the entire failure this guardrail exists to prevent.
        """
        job = _completed_job(client, make_cleaner, make_open_turnover, db)
        url = f"/api/turnovers/{job['turnover']['id']}/pay"

        first = client.post(url, headers=job["owner"]["auth"])
        assert first.status_code == 200, first.text

        # The owner reloads and clicks pay again before the webhook lands.
        second = client.post(url, headers=job["owner"]["auth"])
        assert second.status_code == 200, second.text

        sessions = stripe.paths("/checkout/sessions")
        assert len(sessions) == 2, "both attempts should have reached Stripe"
        keys = {call["idempotency_key"] for call in sessions}
        assert len(keys) == 1, f"a retry must reuse its key, got {keys}"

        award = db.execute(
            select(Award).where(Award.turnover_id == uuid.UUID(job["turnover"]["id"]))
        ).scalar_one()
        assert keys == {f"charge:award:{award.id}"}

        rows = db.execute(
            select(PaymentIn).where(
                PaymentIn.turnover_id == uuid.UUID(job["turnover"]["id"])
            )
        ).scalars().all()
        assert len(rows) == 1, "a retry must not create a second payment row"

    def test_the_key_is_not_a_fresh_value_each_time(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session, stripe
    ) -> None:
        """The same assertion from the other side: the key is *derived*.

        Written separately because it is the thing a future refactor is most
        likely to break silently — a key still goes out, so the call still
        works, and nothing fails until a customer is charged twice.
        """
        job = _completed_job(client, make_cleaner, make_open_turnover, db)
        client.post(f"/api/turnovers/{job['turnover']['id']}/pay", headers=job["owner"]["auth"])

        key = stripe.paths("/checkout/sessions")[0]["idempotency_key"]
        payment = _payment(db, job["turnover"]["id"])
        assert key == payment.idempotency_key
        assert key.startswith("charge:award:")
        # It survives a reread of the row, which a generated value would not.
        assert uuid.UUID(key.rsplit(":", 1)[-1])

    def test_the_attempt_is_written_before_the_call(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session, stripe
    ) -> None:
        """Guardrail 2's other half, asserted at the moment it matters.

        Stripe is made to blow up mid-call. What must survive is a row that
        visibly *tried* — not one that looks untouched, which is what a crash
        before the write would leave.
        """
        job = _completed_job(client, make_cleaner, make_open_turnover, db)
        stripe.failures["/checkout/sessions"] = stripe_client.StripeError(
            "connection reset"
        )

        resp = client.post(
            f"/api/turnovers/{job['turnover']['id']}/pay", headers=job["owner"]["auth"]
        )
        assert resp.status_code == 409

        payment = _payment(db, job["turnover"]["id"])
        assert payment.attempted_at is not None, "the attempt was never recorded"
        assert payment.status is PaymentStatus.REQUIRES_REVIEW
        assert "connection reset" in (payment.failure_message or "")

    def test_an_unknown_outcome_is_never_retried_automatically(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session, stripe
    ) -> None:
        """`requires_review` means a human looks at it, not that we try again.

        Retrying a charge whose outcome nobody knows is how the double bill
        happens even with a key, because the row we would key off is the one we
        are unsure about.
        """
        job = _completed_job(client, make_cleaner, make_open_turnover, db)
        stripe.failures["/checkout/sessions"] = stripe_client.StripeError("timeout")
        client.post(f"/api/turnovers/{job['turnover']['id']}/pay", headers=job["owner"]["auth"])

        stripe.failures.clear()
        again = client.post(
            f"/api/turnovers/{job['turnover']['id']}/pay", headers=job["owner"]["auth"]
        )
        assert again.status_code == 409
        assert "unknown outcome" in again.json()["detail"]
        assert len(stripe.paths("/checkout/sessions")) == 1, "it tried again anyway"

    def test_a_refusal_from_stripe_is_a_failure_not_a_mystery(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session, stripe
    ) -> None:
        """Stripe answering "no" is a known outcome. Only silence is unknown."""
        job = _completed_job(client, make_cleaner, make_open_turnover, db)
        stripe.failures["/checkout/sessions"] = stripe_client.StripeError(
            "Your card was declined.", code="card_declined", status=402
        )
        client.post(f"/api/turnovers/{job['turnover']['id']}/pay", headers=job["owner"]["auth"])

        assert _payment(db, job["turnover"]["id"]).status is PaymentStatus.FAILED


# --------------------------------------------------------------------------
# The destination charge — one transaction, not two ledgers
# --------------------------------------------------------------------------


class TestTheChargeItself:
    def test_it_is_a_destination_charge_with_the_fee_as_the_platform_cut(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session, stripe
    ) -> None:
        """The shape is the invariant.

        A plain charge with no `transfer_data` still succeeds, still takes the
        owner's money, and quietly keeps all of it on the platform — nothing
        errors, the cleaner simply never gets paid. So the request body is
        asserted field by field.
        """
        job = _completed_job(client, make_cleaner, make_open_turnover, db, price_cents=20_000)
        profile = db.execute(
            select(CleanerProfile).where(
                CleanerProfile.user_id == uuid.UUID(job["cleaner"]["user"]["id"])
            )
        ).scalar_one()

        client.post(f"/api/turnovers/{job['turnover']['id']}/pay", headers=job["owner"]["auth"])
        sent = stripe.paths("/checkout/sessions")[0]["data"]

        intent = sent["payment_intent_data"]
        assert intent["transfer_data"]["destination"] == profile.stripe_account_id
        assert intent["application_fee_amount"] == 3_000
        assert sent["line_items"][0]["price_data"]["unit_amount"] == 20_000
        assert sent["mode"] == "payment"

    def test_a_cleaner_who_cannot_be_paid_blocks_the_charge_with_a_reason(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session, stripe
    ) -> None:
        """Taking the owner's money for a transfer that cannot land is worse
        than refusing, because the money is then ours to sort out."""
        job = _completed_job(
            client, make_cleaner, make_open_turnover, db, payout_ready=False
        )
        resp = client.post(
            f"/api/turnovers/{job['turnover']['id']}/pay", headers=job["owner"]["auth"]
        )
        assert resp.status_code == 409
        assert "payout" in resp.json()["detail"].lower()
        assert not stripe.paths("/checkout/sessions")

    def test_a_job_that_is_not_finished_cannot_be_charged_for(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session, stripe
    ) -> None:
        """Money moves for work that happened."""
        job = make_open_turnover()
        cleaner = make_cleaner(cleared=True)
        _payout_ready(db, cleaner)
        bid = _bid(client, cleaner, job["turnover"]["id"])
        client.post(
            f"/api/turnovers/{job['turnover']['id']}/bids/{bid['id']}/accept",
            headers=job["owner"]["auth"],
        )

        resp = client.post(
            f"/api/turnovers/{job['turnover']['id']}/pay", headers=job["owner"]["auth"]
        )
        assert resp.status_code == 409
        assert "not been marked complete" in resp.json()["detail"]

    def test_somebody_elses_turnover_is_not_yours_to_pay_for(
        self, client: TestClient, make_cleaner, make_open_turnover, make_user, db: Session, stripe
    ) -> None:
        job = _completed_job(client, make_cleaner, make_open_turnover, db)
        stranger = make_user(role="owner")
        resp = client.post(
            f"/api/turnovers/{job['turnover']['id']}/pay", headers=stranger["auth"]
        )
        assert resp.status_code == 404

    def test_with_no_stripe_key_the_path_is_off_rather_than_faked(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session, monkeypatch
    ) -> None:
        """A different posture from Checkr and SMTP, on purpose.

        A human can run a background check by hand and a log line can stand in
        for an email. Nothing stands in for money, so no row may claim to have
        collected anything.
        """
        job = _completed_job(client, make_cleaner, make_open_turnover, db)
        monkeypatch.setattr(settings, "stripe_secret_key", None)

        resp = client.post(
            f"/api/turnovers/{job['turnover']['id']}/pay", headers=job["owner"]["auth"]
        )
        assert resp.status_code == 409
        assert "not configured" in resp.json()["detail"]
        assert payments.payment_for(db, uuid.UUID(job["turnover"]["id"])) is None


# --------------------------------------------------------------------------
# Settlement — and the carry-over reconciliation row
# --------------------------------------------------------------------------


def _webhook(client: TestClient, kind: str, obj: dict, *, secret: str = "whsec_test") -> object:
    """Post a properly signed delivery, the way Stripe would."""
    body = json.dumps({"type": kind, "data": {"object": obj}}).encode()
    timestamp = int(time.time())
    signature = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return client.post(
        "/api/stripe/webhook",
        content=body,
        headers={
            "Stripe-Signature": f"t={timestamp},v1={signature}",
            "Content-Type": "application/json",
        },
    )


@pytest.fixture
def webhook_secret(monkeypatch) -> str:
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_test")
    return "whsec_test"


class TestSettlement:
    def _paid(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session, stripe
    ) -> dict:
        job = _completed_job(client, make_cleaner, make_open_turnover, db, price_cents=20_000)
        client.post(f"/api/turnovers/{job['turnover']['id']}/pay", headers=job["owner"]["auth"])
        resp = _webhook(
            client,
            "checkout.session.completed",
            {
                "object": "checkout.session",
                "id": "cs_test_fake",
                "payment_status": "paid",
                "payment_intent": "pi_test_fake",
                "metadata": {"turnover_id": job["turnover"]["id"]},
            },
        )
        assert resp.status_code == 200, resp.text
        return job

    def test_the_money_is_only_true_once_stripe_says_so(
        self, client, make_cleaner, make_open_turnover, db, stripe, webhook_secret
    ) -> None:
        """Starting a checkout is an intent to pay, not a payment."""
        job = _completed_job(client, make_cleaner, make_open_turnover, db)
        client.post(f"/api/turnovers/{job['turnover']['id']}/pay", headers=job["owner"]["auth"])

        payment = _payment(db, job["turnover"]["id"])
        assert payment.status is PaymentStatus.PROCESSING
        assert payments.payout_for(db, payment.turnover_id) is None
        assert not db.execute(
            select(notifications.Notification).where(
                notifications.Notification.event == NotificationEvent.PAYMENT_RECEIPT
            )
        ).scalars().all(), "a receipt went out before the money moved"

    def test_settling_records_the_payout_and_tells_both_sides(
        self, client, make_cleaner, make_open_turnover, db, stripe, webhook_secret
    ) -> None:
        job = self._paid(client, make_cleaner, make_open_turnover, db, stripe)

        payment = _payment(db, job["turnover"]["id"])
        assert payment.status is PaymentStatus.SUCCEEDED
        assert payment.stripe_payment_intent_id == "pi_test_fake"

        payout = payments.payout_for(db, payment.turnover_id)
        assert payout is not None
        assert payout.amount_cents == 17_000
        assert payout.payment_in_id == payment.id

        told = {
            (row.event, row.destination)
            for row in db.execute(select(notifications.Notification)).scalars()
        }
        assert (NotificationEvent.PAYMENT_RECEIPT, job["owner"]["user"]["email"]) in told
        assert (NotificationEvent.PAYOUT_NOTICE, job["cleaner"]["user"]["email"]) in told

    def test_the_transfer_reference_is_read_back_and_recorded(
        self, client, make_cleaner, make_open_turnover, db, stripe, webhook_secret
    ) -> None:
        """The session does not carry it, so it is fetched from the charge.

        Without the reference a refund that has to reverse the transfer has
        nothing to point at, which is precisely the moment somebody is arguing
        about money.
        """
        stripe.responses["/payment_intents/pi_test_fake?expand[]=latest_charge"] = {
            "id": "pi_test_fake",
            "latest_charge": {"id": "ch_test_fake", "transfer": "tr_test_fake"},
        }
        job = self._paid(client, make_cleaner, make_open_turnover, db, stripe)

        payout = payments.payout_for(db, uuid.UUID(job["turnover"]["id"]))
        assert payout.stripe_transfer_id == "tr_test_fake"

    def test_a_missing_transfer_reference_does_not_fail_the_webhook(
        self, client, make_cleaner, make_open_turnover, db, stripe, webhook_secret
    ) -> None:
        """The money already moved. Failing here would have Stripe retry forever."""
        stripe.failures["/payment_intents/pi_test_fake?expand[]=latest_charge"] = (
            stripe_client.StripeError("upstream hiccup")
        )
        job = self._paid(client, make_cleaner, make_open_turnover, db, stripe)

        payment = _payment(db, job["turnover"]["id"])
        assert payment.status is PaymentStatus.SUCCEEDED
        assert payments.payout_for(db, payment.turnover_id) is not None

    def test_a_redelivered_webhook_does_not_pay_anybody_twice(
        self, client, make_cleaner, make_open_turnover, db, stripe, webhook_secret
    ) -> None:
        """Stripe retries until it gets a 2xx. That is normal, not an attack."""
        job = self._paid(client, make_cleaner, make_open_turnover, db, stripe)
        _webhook(
            client,
            "checkout.session.completed",
            {
                "object": "checkout.session",
                "id": "cs_test_fake",
                "payment_status": "paid",
                "payment_intent": "pi_test_fake",
                "metadata": {"turnover_id": job["turnover"]["id"]},
            },
        )

        db.expire_all()
        payouts = db.execute(
            select(Payout).where(Payout.turnover_id == uuid.UUID(job["turnover"]["id"]))
        ).scalars().all()
        assert len(payouts) == 1

        receipts = db.execute(
            select(notifications.Notification).where(
                notifications.Notification.event == NotificationEvent.PAYMENT_RECEIPT
            )
        ).scalars().all()
        assert len(receipts) == 1, "the owner got two receipts for one payment"

    def test_collected_equals_paid_out_plus_the_fee(
        self, client, make_cleaner, make_open_turnover, db, stripe, webhook_secret
    ) -> None:
        """**The carry-over reconciliation test.** No drift, ever."""
        job = self._paid(client, make_cleaner, make_open_turnover, db, stripe)

        payment = _payment(db, job["turnover"]["id"])
        payout = payments.payout_for(db, payment.turnover_id)
        books = payments.reconcile(payment, payout)

        assert books["collected_cents"] == 20_000
        assert books["paid_out_cents"] == 17_000
        assert books["platform_fee_cents"] == 3_000
        assert books["drift_cents"] == 0

    @pytest.mark.parametrize("price", [1_999, 4_501, 7_333, 25_000, 100_001])
    def test_it_reconciles_at_any_price(
        self, client, make_cleaner, make_open_turnover, db, stripe, webhook_secret, price
    ) -> None:
        """One worked example proves arithmetic; a range proves the property."""
        job = _completed_job(client, make_cleaner, make_open_turnover, db, price_cents=price)
        client.post(f"/api/turnovers/{job['turnover']['id']}/pay", headers=job["owner"]["auth"])
        _webhook(
            client,
            "checkout.session.completed",
            {
                "object": "checkout.session",
                "id": "cs_test_fake",
                "payment_status": "paid",
                "payment_intent": "pi_test_fake",
                "metadata": {"turnover_id": job["turnover"]["id"]},
            },
        )

        payment = _payment(db, job["turnover"]["id"])
        books = payments.reconcile(payment, payments.payout_for(db, payment.turnover_id))
        assert books["drift_cents"] == 0
        assert books["collected_cents"] == price

    def test_a_failed_payment_is_said_out_loud(
        self, client, make_cleaner, make_open_turnover, db, stripe, webhook_secret
    ) -> None:
        job = _completed_job(client, make_cleaner, make_open_turnover, db)
        client.post(f"/api/turnovers/{job['turnover']['id']}/pay", headers=job["owner"]["auth"])
        _webhook(
            client,
            "payment_intent.payment_failed",
            {
                "object": "payment_intent",
                "id": "pi_test_fake",
                "last_payment_error": {"message": "Your card was declined."},
                "metadata": {"turnover_id": job["turnover"]["id"]},
            },
        )

        payment = _payment(db, job["turnover"]["id"])
        assert payment.status is PaymentStatus.FAILED
        assert "declined" in (payment.failure_message or "")

    def test_a_late_failure_does_not_undo_a_recorded_success(
        self, client, make_cleaner, make_open_turnover, db, stripe, webhook_secret
    ) -> None:
        """Webhooks arrive out of order. A success already recorded stands."""
        job = self._paid(client, make_cleaner, make_open_turnover, db, stripe)
        _webhook(
            client,
            "payment_intent.payment_failed",
            {
                "object": "payment_intent",
                "id": "pi_test_fake",
                "last_payment_error": {"message": "an earlier attempt"},
                "metadata": {"turnover_id": job["turnover"]["id"]},
            },
        )
        assert _payment(db, job["turnover"]["id"]).status is PaymentStatus.SUCCEEDED


class TestTheWebhookIsSigned:
    def test_an_unsigned_delivery_is_refused(
        self, client: TestClient, webhook_secret
    ) -> None:
        """A POST that says a payment succeeded, from anyone, is from anyone."""
        resp = client.post(
            "/api/stripe/webhook",
            content=b'{"type":"checkout.session.completed"}',
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_a_forged_signature_is_refused(
        self, client: TestClient, webhook_secret
    ) -> None:
        body = b'{"type":"checkout.session.completed","data":{"object":{}}}'
        timestamp = int(time.time())
        forged = hmac.new(b"not-the-secret", f"{timestamp}.".encode() + body, hashlib.sha256)
        resp = client.post(
            "/api/stripe/webhook",
            content=body,
            headers={"Stripe-Signature": f"t={timestamp},v1={forged.hexdigest()}"},
        )
        assert resp.status_code == 400

    def test_a_replayed_delivery_from_last_week_is_refused(
        self, client: TestClient, webhook_secret
    ) -> None:
        body = b'{"type":"checkout.session.completed","data":{"object":{}}}'
        stale = int(time.time()) - 60 * 60 * 24 * 7
        signature = hmac.new(
            b"whsec_test", f"{stale}.".encode() + body, hashlib.sha256
        ).hexdigest()
        resp = client.post(
            "/api/stripe/webhook",
            content=body,
            headers={"Stripe-Signature": f"t={stale},v1={signature}"},
        )
        assert resp.status_code == 400

    def test_with_no_secret_configured_everything_is_refused(
        self, client: TestClient, monkeypatch
    ) -> None:
        """Refusing is the safe default. Trusting an unsigned POST is not."""
        monkeypatch.setattr(settings, "stripe_webhook_secret", None)
        body = b'{"type":"checkout.session.completed","data":{"object":{}}}'
        timestamp = int(time.time())
        signature = hmac.new(
            b"whsec_test", f"{timestamp}.".encode() + body, hashlib.sha256
        ).hexdigest()
        resp = client.post(
            "/api/stripe/webhook",
            content=body,
            headers={"Stripe-Signature": f"t={timestamp},v1={signature}"},
        )
        assert resp.status_code == 400


# --------------------------------------------------------------------------
# Refunds — the carry-over row about nothing being left stranded
# --------------------------------------------------------------------------


class TestRefunds:
    def _paid_job(self, client, make_cleaner, make_open_turnover, db, stripe) -> dict:
        job = _completed_job(client, make_cleaner, make_open_turnover, db, price_cents=20_000)
        client.post(f"/api/turnovers/{job['turnover']['id']}/pay", headers=job["owner"]["auth"])
        _webhook(
            client,
            "checkout.session.completed",
            {
                "object": "checkout.session",
                "id": "cs_test_fake",
                "payment_status": "paid",
                "payment_intent": "pi_test_fake",
                "metadata": {"turnover_id": job["turnover"]["id"]},
            },
        )
        return job

    def test_the_fee_comes_back_and_the_transfer_is_reversed(
        self, client, make_cleaner, make_open_turnover, admin_user, db, stripe, webhook_secret
    ) -> None:
        """**The carry-over refund test.** Both halves resolve, or it is wrong.

        Refunding a destination charge is not a reversal. Two decisions have to
        be made explicitly, and both are asserted on the wire:

        * the application fee comes back — we did not earn a cut of a cleaning
          the owner is being refunded for;
        * the transfer is reversed — without it the owner is made whole out of
          the platform's own balance while the cleaner keeps the full amount,
          and *nothing errors*, which is what makes it dangerous.
        """
        job = self._paid_job(client, make_cleaner, make_open_turnover, db, stripe)
        admin = admin_user

        resp = client.post(
            f"/api/turnovers/{job['turnover']['id']}/refund",
            json={"reason": "Owner disputed the clean; agreed to refund."},
            headers=admin["auth"],
        )
        assert resp.status_code == 200, resp.text

        sent = stripe.paths("/refunds")[0]
        assert sent["data"]["refund_application_fee"] is True
        assert sent["data"]["reverse_transfer"] is True
        assert sent["data"]["payment_intent"] == "pi_test_fake"

        payment = _payment(db, job["turnover"]["id"])
        assert sent["idempotency_key"] == f"refund:payment:{payment.id}"

    def test_nothing_is_left_stranded_after_a_refund(
        self, client, make_cleaner, make_open_turnover, admin_user, db, stripe, webhook_secret
    ) -> None:
        """Reconciliation still balances at zero once the money goes back."""
        job = self._paid_job(client, make_cleaner, make_open_turnover, db, stripe)
        admin = admin_user
        client.post(
            f"/api/turnovers/{job['turnover']['id']}/refund",
            json={"reason": "Job was not done."},
            headers=admin["auth"],
        )

        payment = _payment(db, job["turnover"]["id"])
        payout = payments.payout_for(db, payment.turnover_id)
        db.refresh(payout)
        books = payments.reconcile(payment, payout)

        assert payment.status is PaymentStatus.REFUNDED
        assert payment.refunded_amount_cents == 20_000
        assert payout.reversed_amount_cents == 17_000
        assert books == {
            "collected_cents": 0,
            "paid_out_cents": 0,
            "platform_fee_cents": 0,
            "drift_cents": 0,
        }

    def test_the_payout_row_is_kept_rather_than_deleted(
        self, client, make_cleaner, make_open_turnover, admin_user, db, stripe, webhook_secret
    ) -> None:
        """What was paid and then clawed back is the history a dispute needs —
        the same reason a cancelled award is cancelled rather than deleted."""
        job = self._paid_job(client, make_cleaner, make_open_turnover, db, stripe)
        admin = admin_user
        client.post(
            f"/api/turnovers/{job['turnover']['id']}/refund",
            json={"reason": "Disputed."},
            headers=admin["auth"],
        )

        payout = payments.payout_for(db, uuid.UUID(job["turnover"]["id"]))
        assert payout is not None, "the payout row was deleted"
        assert payout.amount_cents == 17_000, "the original amount is still readable"

    def test_refunding_twice_is_refused(
        self, client, make_cleaner, make_open_turnover, admin_user, db, stripe, webhook_secret
    ) -> None:
        job = self._paid_job(client, make_cleaner, make_open_turnover, db, stripe)
        admin = admin_user
        body = {"reason": "Disputed."}
        first = client.post(
            f"/api/turnovers/{job['turnover']['id']}/refund", json=body, headers=admin["auth"]
        )
        assert first.status_code == 200
        second = client.post(
            f"/api/turnovers/{job['turnover']['id']}/refund", json=body, headers=admin["auth"]
        )
        assert second.status_code == 409
        assert len(stripe.paths("/refunds")) == 1

    def test_the_refund_attempt_is_flagged_before_the_call(
        self, client, make_cleaner, make_open_turnover, admin_user, db, stripe, webhook_secret
    ) -> None:
        """Guardrail 2 on the refund, not only on the charge.

        A timestamp alone was not enough here. A process killed mid-refund
        would leave a row reading "paid, never refunded" while Stripe had
        already refunded and reversed the transfer — and `reconcile()` reads
        that row and reports the books balanced. The one instrument that would
        catch it would have said everything was fine.
        """
        job = self._paid_job(client, make_cleaner, make_open_turnover, db, stripe)
        stripe.failures["/refunds"] = stripe_client.StripeError("connection reset")

        resp = client.post(
            f"/api/turnovers/{job['turnover']['id']}/refund",
            json={"reason": "Disputed."},
            headers=admin_user["auth"],
        )
        assert resp.status_code == 409

        payment = _payment(db, job["turnover"]["id"])
        assert payment.status is PaymentStatus.REQUIRES_REVIEW
        assert payment.status is not PaymentStatus.SUCCEEDED, (
            "a refund with an unknown outcome still reads as a settled payment"
        )

    def test_a_refused_refund_does_not_erase_the_collection(
        self, client, make_cleaner, make_open_turnover, admin_user, db, stripe,
        webhook_secret,
    ) -> None:
        """**A failed refund is not a failed collection.**

        Stripe answering with a definitive error means the refund did not
        happen: the charge is still collected, the transfer still out, and the
        row still carries both. Writing `failed` said the opposite, and the
        ledger believed it — zero revenue, zero fee, and the cleaner's payout
        reported as negative drift. The one screen that exists to say what
        moved would have misstated the books every time a refund was refused.

        `mark_failed` already holds this principle for webhooks arriving out of
        order: a success already recorded is not undone by a later failure
        notice. It applies just as much to a failure this code writes itself.
        """
        job = self._paid_job(client, make_cleaner, make_open_turnover, db, stripe)
        stripe.failures["/refunds"] = stripe_client.StripeError(
            "charge already refunded", code="charge_already_refunded", status=400
        )

        resp = client.post(
            f"/api/turnovers/{job['turnover']['id']}/refund",
            json={"reason": "Disputed."},
            headers=admin_user["auth"],
        )
        assert resp.status_code == 409

        payment = _payment(db, job["turnover"]["id"])
        assert payment.status is PaymentStatus.SUCCEEDED, (
            "the collection is still in force; only the refund failed"
        )
        assert "refund failed" in (payment.failure_message or ""), (
            "and the attempt has to stay visible on the row"
        )
        assert payment.refunded_amount_cents == 0

        body = client.get("/api/admin/ledger", headers=admin_user["auth"]).json()
        assert body["total_collected_cents"] == 20_000
        assert (
            body["total_collected_cents"]
            == body["total_paid_out_cents"] + body["total_platform_fee_cents"]
        )
        assert body["total_drift_cents"] == 0, (
            "a refused refund made the books look like money had gone out "
            "with nothing ever collected"
        )

    def test_a_refunded_turnover_is_not_quietly_re_chargeable(
        self, client, make_cleaner, make_open_turnover, admin_user, db, stripe, webhook_secret
    ) -> None:
        """The derived key cuts both ways, and this is the edge of it.

        Paying again after a refund would send the *same* key as the original
        charge, so Stripe replays the refunded session rather than raising a
        new one: money appears to move and does not. Re-charging after a refund
        is a decision, not a retry.
        """
        job = self._paid_job(client, make_cleaner, make_open_turnover, db, stripe)
        client.post(
            f"/api/turnovers/{job['turnover']['id']}/refund",
            json={"reason": "Disputed."},
            headers=admin_user["auth"],
        )

        before = len(stripe.paths("/checkout/sessions"))
        again = client.post(
            f"/api/turnovers/{job['turnover']['id']}/pay", headers=job["owner"]["auth"]
        )
        assert again.status_code == 409
        assert "manual decision" in again.json()["detail"]
        assert len(stripe.paths("/checkout/sessions")) == before

    def test_an_owner_cannot_refund_themselves(
        self, client, make_cleaner, make_open_turnover, db, stripe, webhook_secret
    ) -> None:
        """A refund reverses a transfer somebody was told they earned, so it is
        a human decision in the dispute inbox, not a button on a screen."""
        job = self._paid_job(client, make_cleaner, make_open_turnover, db, stripe)
        resp = client.post(
            f"/api/turnovers/{job['turnover']['id']}/refund",
            json={"reason": "Changed my mind."},
            headers=job["owner"]["auth"],
        )
        assert resp.status_code == 403
        assert not stripe.paths("/refunds")

    def test_a_reason_is_required(
        self, client, make_cleaner, make_open_turnover, admin_user, db, stripe, webhook_secret
    ) -> None:
        job = self._paid_job(client, make_cleaner, make_open_turnover, db, stripe)
        admin = admin_user
        resp = client.post(
            f"/api/turnovers/{job['turnover']['id']}/refund",
            json={},
            headers=admin["auth"],
        )
        assert resp.status_code == 422


# --------------------------------------------------------------------------
# Connect onboarding, and the gate it is deliberately not part of
# --------------------------------------------------------------------------


class TestPayoutSetup:
    def test_onboarding_creates_one_express_account_and_remembers_it(
        self, client: TestClient, make_cleaner, db: Session, stripe
    ) -> None:
        cleaner = make_cleaner(cleared=True)
        first = client.post("/api/payouts/onboarding", headers=cleaner["auth"])
        assert first.status_code == 200, first.text
        assert first.json()["url"] == "https://connect.stripe.test/setup"

        client.post("/api/payouts/onboarding", headers=cleaner["auth"])

        accounts = stripe.paths("/accounts")
        assert len(accounts) == 1, "a second click created a second account"
        assert accounts[0]["data"]["type"] == "express", "Custom puts 1099s on us"
        assert accounts[0]["idempotency_key"].startswith("connect:account:")

    def test_the_link_call_signs_its_reason_for_having_no_key(
        self, client: TestClient, make_cleaner, db: Session, stripe
    ) -> None:
        """The one exception in the codebase, and it has to say why.

        An account link expires and must be re-issued; reusing a key would hand
        the cleaner back a dead URL. It moves no money, which is the only reason
        it is allowed to skip the key — and skipping it is a signed, greppable
        act rather than an omission.
        """
        cleaner = make_cleaner(cleared=True)
        client.post("/api/payouts/onboarding", headers=cleaner["auth"])

        link = stripe.paths("/account_links")[0]
        assert link["idempotency_key"] is None
        assert "move no money" in link["non_idempotent_reason"]

    def test_payout_readiness_is_stripes_answer_not_ours(
        self, client: TestClient, make_cleaner, db: Session, stripe
    ) -> None:
        """Finishing the form and passing verification are different events."""
        cleaner = make_cleaner(cleared=True)
        client.post("/api/payouts/onboarding", headers=cleaner["auth"])

        stripe.responses["/accounts/acct_fake"] = {
            "id": "acct_fake",
            "details_submitted": True,
            "payouts_enabled": False,
        }
        resp = client.get("/api/payouts/status?refresh=true", headers=cleaner["auth"])
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["details_submitted"] is True
        assert body["payouts_enabled"] is False
        assert "verifying" in body["blocker"]

    def test_a_cleaner_with_no_payout_account_can_still_bid(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session, stripe
    ) -> None:
        """**The trust gate has exactly one author, and this is not it.**

        Being trusted in a stranger's house and being able to receive a transfer
        are different questions. Folding Stripe readiness into `can_take_jobs`
        would mean a verification delay silently stops a vetted cleaner from
        bidding, with the badge and the gate disagreeing about why.
        """
        job = make_open_turnover()
        cleaner = make_cleaner(cleared=True)
        assert cleaner["profile"].stripe_account_id is None

        resp = client.put(
            f"/api/board/{job['turnover']['id']}/bid",
            json={"price_cents": 12_000},
            headers=cleaner["auth"],
        )
        assert resp.status_code == 200, "vetting clears bidding; payouts are separate"

    def test_the_status_endpoint_says_when_payments_are_off(
        self, client: TestClient, make_cleaner, db: Session, monkeypatch
    ) -> None:
        monkeypatch.setattr(settings, "stripe_secret_key", None)
        cleaner = make_cleaner(cleared=True)
        resp = client.get("/api/payouts/status", headers=cleaner["auth"])
        assert resp.json()["payments_configured"] is False


# --------------------------------------------------------------------------
# The wire format — a mis-encoded nested key is silent, so it is tested
# --------------------------------------------------------------------------


class TestFormEncoding:
    def test_nested_values_use_stripes_bracket_syntax(self) -> None:
        """A wrong nested key is not an error to Stripe — it is ignored.

        Which means `transfer_data[destination]` encoded wrongly turns a
        destination charge into a plain one that keeps the cleaner's money on
        the platform, with a 200 and no complaint from anybody.
        """
        pairs = dict(
            stripe_client.encode_form(
                {
                    "mode": "payment",
                    "payment_intent_data": {
                        "application_fee_amount": 3_000,
                        "transfer_data": {"destination": "acct_123"},
                    },
                }
            )
        )
        assert pairs["payment_intent_data[transfer_data][destination]"] == "acct_123"
        assert pairs["payment_intent_data[application_fee_amount]"] == "3000"

    def test_lists_are_indexed_the_way_line_items_need(self) -> None:
        pairs = dict(
            stripe_client.encode_form(
                {"line_items": [{"quantity": 1, "price_data": {"currency": "usd"}}]}
            )
        )
        assert pairs["line_items[0][quantity]"] == "1"
        assert pairs["line_items[0][price_data][currency]"] == "usd"

    def test_booleans_are_lowercase_and_nothing_becomes_a_float(self) -> None:
        pairs = dict(
            stripe_client.encode_form({"reverse_transfer": True, "amount": 1_999})
        )
        assert pairs["reverse_transfer"] == "true"
        assert pairs["amount"] == "1999"
        assert "." not in pairs["amount"]

    def test_a_mutating_call_without_a_key_or_a_reason_is_refused(
        self, monkeypatch
    ) -> None:
        """The door will not open without one or the other."""
        monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_fake")
        with pytest.raises(ValueError, match="idempotency key"):
            stripe_client.post("/refunds", {"payment_intent": "pi_1"})


class TestUnconfigured:
    def test_every_call_raises_rather_than_pretending(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "stripe_secret_key", None)
        with pytest.raises(stripe_client.StripeNotConfigured):
            stripe_client.post("/refunds", {}, idempotency_key="refund:payment:1")
        with pytest.raises(stripe_client.StripeNotConfigured):
            stripe_client.get("/accounts/acct_1")


# --------------------------------------------------------------------------
# The ledger an admin reads (phase 8)
# --------------------------------------------------------------------------



class TestALocalRefusalIsNotAnUnknownOutcome:
    """**A refusal raised before the request leaves this process.**

    Phase 9's live-mode gate refuses when a live key is configured without
    `STRIPE_PLATFORM_ENTITY`. Nothing moved, so whatever the caller wrote
    beforehand is still exactly true — but both handlers keyed on `exc.status`,
    a proxy for "Stripe answered", and a local refusal has no status while
    being the most definite outcome there is.

    Read through that proxy it looked *unknown*, and guardrail 2 treats an
    unknown outcome as permanently unsafe. The gate meant to protect the entity
    would have quietly made jobs unpayable and moved collected money into the
    ledger's unknown bucket. `outcome_known` is the question the handlers were
    really asking.
    """

    def _completed(self, client, make_cleaner, make_open_turnover, db):
        return _completed_job(
            client, make_cleaner, make_open_turnover, db, price_cents=20_000
        )

    def test_a_refused_checkout_stays_payable(
        self, client, make_cleaner, make_open_turnover, db: Session, stripe,
    ) -> None:
        """`start_checkout` refuses outright to re-charge a `requires_review`
        row — correctly, for a genuinely unknown outcome. So marking a local
        refusal that way makes the job unpayable for good, and a person has to
        edit the database to undo it.

        Raised through the fake, the way every other failure in this file is:
        what is under test is the handler's mapping, not the gate itself (which
        `test_launch.py` covers, and which this fixture replaces `post` to
        bypass).
        """
        job = self._completed(client, make_cleaner, make_open_turnover, db)
        turnover_id = job["turnover"]["id"]

        stripe.failures["/checkout/sessions"] = (
            stripe_client.StripeLiveModeUndeclared()
        )
        refused = client.post(
            f"/api/turnovers/{turnover_id}/pay", headers=job["owner"]["auth"]
        )
        assert refused.status_code == 409, refused.text
        assert "STRIPE_PLATFORM_ENTITY" in refused.text

        row = _payment(db, turnover_id)
        assert row.status is not PaymentStatus.REQUIRES_REVIEW, (
            "a refusal that never reached Stripe was recorded as an unknown "
            "outcome, which blocks this turnover from ever being paid"
        )
        assert row.status is PaymentStatus.FAILED

        # The entity is declared and the owner tries again. It must go through.
        stripe.failures.clear()
        again = client.post(
            f"/api/turnovers/{turnover_id}/pay", headers=job["owner"]["auth"]
        )
        assert again.status_code == 200, again.text

    def test_a_refused_refund_leaves_the_collection_intact(
        self, client, make_cleaner, make_open_turnover, db: Session, admin_user,
        stripe, webhook_secret,
    ) -> None:
        """**The same misstatement, through a different door.**

        Turning a settled payment into `requires_review` moves real collected
        revenue into the ledger's `unknown_cents` bucket — exactly what the
        four-bucket split was built to prevent.
        """
        job = self._completed(client, make_cleaner, make_open_turnover, db)
        turnover_id = job["turnover"]["id"]
        client.post(f"/api/turnovers/{turnover_id}/pay", headers=job["owner"]["auth"])
        paid = _webhook(
            client,
            "checkout.session.completed",
            {
                "object": "checkout.session",
                "id": "cs_test_gate",
                "payment_status": "paid",
                "payment_intent": "pi_test_gate",
                "metadata": {"turnover_id": turnover_id},
            },
        )
        assert paid.status_code == 200, paid.text

        stripe.failures["/refunds"] = stripe_client.StripeLiveModeUndeclared()
        refused = client.post(
            f"/api/turnovers/{turnover_id}/refund",
            json={"reason": "Disputed."},
            headers=admin_user["auth"],
        )
        assert refused.status_code == 409, refused.text

        row = _payment(db, turnover_id)
        assert row.status is PaymentStatus.SUCCEEDED, (
            "a refund refused before it left the process turned a collected "
            "payment into money the ledger calls unknown"
        )
        assert row.refunded_amount_cents == 0

        body = client.get("/api/admin/ledger", headers=admin_user["auth"]).json()
        assert body["total_collected_cents"] == 20_000
        assert body["total_unknown_cents"] == 0


class TestTheAdminLedger:
    def test_the_ledger_never_drifts_on_a_real_settled_job(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session,
        admin_user, stripe, webhook_secret,
    ) -> None:
        """Collected equals paid out plus the fee kept — the carry-over check
        from day one, asserted again where an admin reads it.

        **With a real settled payment in the table**, because the same
        assertion over an empty ledger passes without testing anything, which
        is the failure mode this suite has already hit more than once.
        """
        job = _completed_job(
            client, make_cleaner, make_open_turnover, db, price_cents=20_000
        )
        client.post(
            f"/api/turnovers/{job['turnover']['id']}/pay", headers=job["owner"]["auth"]
        )
        paid = _webhook(
            client,
            "checkout.session.completed",
            {
                "object": "checkout.session",
                "id": "cs_test_ledger",
                "payment_status": "paid",
                "payment_intent": "pi_test_ledger",
                "metadata": {"turnover_id": job["turnover"]["id"]},
            },
        )
        assert paid.status_code == 200, paid.text

        body = client.get("/api/admin/ledger", headers=admin_user["auth"]).json()
        assert body["rows"], "the settled job has to appear, or this proves nothing"
        assert body["total_collected_cents"] == 20_000
        assert (
            body["total_collected_cents"]
            == body["total_paid_out_cents"] + body["total_platform_fee_cents"]
        )
        assert body["total_drift_cents"] == 0

    def test_a_checkout_in_flight_is_not_money_collected(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session,
        admin_user, stripe,
    ) -> None:
        """**A payment is only true when Stripe says so** (CLAUDE.md).

        Starting a checkout writes a `PaymentIn` with the intended amount and
        no payout. Reconciled as though it had settled, that reports the whole
        cleaner share as drift and counts money as collected that Stripe has
        not told us about — the console's financial alarm firing for every
        ordinary payment in flight, on the one screen whose job is saying what
        actually moved. An alarm that is usually wrong is one nobody reads.
        """
        job = _completed_job(
            client, make_cleaner, make_open_turnover, db, price_cents=20_000
        )
        started = client.post(
            f"/api/turnovers/{job['turnover']['id']}/pay",
            headers=job["owner"]["auth"],
        )
        assert started.status_code == 200, started.text
        # Deliberately no webhook: this is the window between the owner opening
        # Stripe's page and the money actually moving.

        body = client.get("/api/admin/ledger", headers=admin_user["auth"]).json()
        row = next(r for r in body["rows"] if r["turnover_id"] == job["turnover"]["id"])

        assert row["status"] in {"pending", "processing"}, row["status"]
        assert row["collected_cents"] == 0
        assert row["drift_cents"] == 0
        assert row["awaiting_cents"] == 20_000, (
            "the intended amount is still worth showing — it just is not a "
            "collection"
        )
        assert body["total_collected_cents"] == 0
        assert body["total_drift_cents"] == 0
        assert body["total_awaiting_cents"] == 20_000

    def test_a_failed_payment_is_not_drift(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session,
        admin_user, stripe,
    ) -> None:
        """Nothing was collected, so nothing is out of balance."""
        job = _completed_job(
            client, make_cleaner, make_open_turnover, db, price_cents=15_000
        )
        client.post(
            f"/api/turnovers/{job['turnover']['id']}/pay", headers=job["owner"]["auth"]
        )

        row = db.execute(
            select(PaymentIn).where(
                PaymentIn.turnover_id == uuid.UUID(job["turnover"]["id"])
            )
        ).scalars().one()
        row.status = PaymentStatus.FAILED
        row.failure_message = "card_declined"
        db.commit()

        body = client.get("/api/admin/ledger", headers=admin_user["auth"]).json()
        assert body["total_collected_cents"] == 0
        assert body["total_drift_cents"] == 0
        assert body["total_awaiting_cents"] == 0

    def test_an_unknown_outcome_is_counted_as_neither(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session,
        admin_user, stripe,
    ) -> None:
        """**Guardrail 2's flag, reported rather than resolved.**

        `requires_review` is written before the network call so a process that
        dies mid-charge leaves a visible row. Counting it as collected claims
        money nobody has confirmed; counting it as zero quietly writes off
        money that may well have moved. It gets its own number and a person
        decides.
        """
        job = _completed_job(
            client, make_cleaner, make_open_turnover, db, price_cents=18_000
        )
        client.post(
            f"/api/turnovers/{job['turnover']['id']}/pay", headers=job["owner"]["auth"]
        )

        row = db.execute(
            select(PaymentIn).where(
                PaymentIn.turnover_id == uuid.UUID(job["turnover"]["id"])
            )
        ).scalars().one()
        row.status = PaymentStatus.REQUIRES_REVIEW
        db.commit()

        body = client.get("/api/admin/ledger", headers=admin_user["auth"]).json()
        assert body["total_collected_cents"] == 0
        assert body["total_drift_cents"] == 0
        assert body["total_awaiting_cents"] == 0, (
            "an unknown outcome is not the same as one in flight"
        )
        assert body["total_unknown_cents"] == 18_000

    def test_a_payout_against_an_unsettled_payment_still_alarms(
        self, client: TestClient, make_cleaner, make_open_turnover, db: Session,
        admin_user, stripe,
    ) -> None:
        """**Zeroing the row must not zero the alarm.**

        Money out with nothing in is exactly what drift is for. Suppressing
        unsettled rows entirely would hide the one case worth shouting about,
        so collected is zero rather than absent and the subtraction still runs.
        """
        job = _completed_job(
            client, make_cleaner, make_open_turnover, db, price_cents=12_000
        )
        client.post(
            f"/api/turnovers/{job['turnover']['id']}/pay", headers=job["owner"]["auth"]
        )

        turnover_id = uuid.UUID(job["turnover"]["id"])
        payment = db.execute(
            select(PaymentIn).where(PaymentIn.turnover_id == turnover_id)
        ).scalars().one()
        payment.status = PaymentStatus.FAILED
        award = db.execute(
            select(Award).where(Award.turnover_id == turnover_id)
        ).scalars().first()
        db.add(
            Payout(
                turnover_id=turnover_id,
                cleaner_id=award.cleaner_id,
                amount_cents=11_000,
                status=PaymentStatus.SUCCEEDED,
            )
        )
        db.commit()

        body = client.get("/api/admin/ledger", headers=admin_user["auth"]).json()
        assert body["total_drift_cents"] == -11_000, (
            "a cleaner was paid for a payment that never succeeded, and the "
            "ledger said nothing"
        )
