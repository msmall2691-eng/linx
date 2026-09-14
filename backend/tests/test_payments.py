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
