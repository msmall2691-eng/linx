"""Phase 9 — is this ready to take real money from real people?

**A checklist this repository is willing to trust**, which means one that runs,
reads the live configuration and the live database, and refuses to call
anything ready that it could not actually verify.

The assertions below are mostly about the fourth state. `UNVERIFIABLE` is the
design decision worth testing hardest: the obvious implementation has two
states, pass and fail, and quietly turns everything it cannot see into a pass —
so it reports all-green for a system nobody has confirmed anything about. That
is the same mistake as assuming an unknown Stripe outcome succeeded, and it is
refused here for the same reason.
"""

from __future__ import annotations

import os
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import UserRole
from app.models.task_run import TaskRun
from app.services import delivery, launch, payments, stripe_client
from app.services.launch import State


def _by_key(checks, key):
    return next(c for c in checks if c.key == key)


def _states(checks):
    return {c.key: c.state for c in checks}


@pytest.fixture
def ready_env(monkeypatch, tmp_path):
    """Everything a check reads, set to the state a real launch would have.

    Individual tests knock one thing back out, which is what makes each
    assertion about that one thing rather than about the fixture.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "secret_key", "x7Qp" * 9)
    monkeypatch.setattr(settings, "cors_origins", "https://linx.example")
    monkeypatch.setattr(settings, "document_storage_dir", str(tmp_path))
    monkeypatch.setattr(settings, "smtp_host", "smtp.example")
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_x")
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_x")
    monkeypatch.setattr(settings, "public_base_url", "https://linx.example")
    monkeypatch.setattr(settings, "stripe_platform_entity", None)
    # `ismount` is the real test for the volume and a tmp_path is not one.
    monkeypatch.setattr(os.path, "ismount", lambda p: str(p) == str(tmp_path))
    return settings


class TestAChecklistThatCannotLie:
    def test_it_never_reports_launchable_while_something_is_unverifiable(
        self, db: Session, ready_env, admin_user
    ) -> None:
        """**The point of the fourth state.**

        Three items here can never be checked from inside this process — whose
        EIN the Stripe account was opened under, whether anybody has walked a
        candidate through Checkr, whether a human actually works the dispute
        inbox. A two-state checklist calls all three a pass and says ready.
        """
        _record_scheduled(db)
        checks = launch.run_checks(db)

        assert not launch.blocking(checks), [c.key for c in launch.blocking(checks)]
        assert not launch.is_launchable(checks), (
            "nothing was blocking, so the list said ready — while three items "
            "had not been looked at by anyone"
        )
        asks = [c for c in checks if c.state is State.UNVERIFIABLE]
        assert {c.key for c in asks} >= {"entity_separation", "background_checks"}
        assert all(c in launch.outstanding(checks) for c in asks), (
            "an item nobody can check must count as outstanding, not as done"
        )

    def test_every_failure_names_what_to_do(self, db: Session, ready_env) -> None:
        """A check that says no without saying what to do is a check somebody
        learns to scroll past."""
        checks = launch.run_checks(db)
        for check in checks:
            if check.state is State.READY:
                continue
            assert check.remedy or check.state is State.ATTENTION, (
                f"{check.key} is not ready and offers no remedy"
            )


class TestTheThingsThatStopSilently:
    def test_a_storage_directory_that_is_not_a_volume_blocks(
        self, db: Session, ready_env, monkeypatch, tmp_path
    ) -> None:
        """**The README has said this since phase 3 and nothing checked it.**

        A plain directory is writable, exists, and passes every naive test —
        and is replaced on the next deploy, so the uploaded photo IDs vanish
        while their rows survive. The failure shows up one deploy after the
        mistake, which is why a person never connects the two.
        """
        monkeypatch.setattr(os.path, "ismount", lambda p: False)
        check = _by_key(launch.run_checks(db), "document_storage")
        assert check.state is State.BLOCKED
        assert "mount point" in check.detail

    def test_no_admin_means_every_admin_alert_reaches_nobody(
        self, db: Session, ready_env
    ) -> None:
        """`notifications` resolves the admin from the *role* rather than an
        address, on purpose. With no admin at all that resolves to an empty
        list and every alert is addressed to nobody — silently."""
        check = _by_key(launch.run_checks(db), "admin")
        assert check.state is State.BLOCKED

        from app.core.security import hash_password
        from app.models.user import User

        db.add(
            User(
                email="console@example.com",
                hashed_password=hash_password("x"),
                full_name="Console",
                role=UserRole.ADMIN,
            )
        )
        db.commit()
        assert _by_key(launch.run_checks(db), "admin").state is State.READY

    def test_no_mail_host_blocks_because_nothing_is_ever_sent(
        self, db: Session, ready_env, monkeypatch
    ) -> None:
        monkeypatch.setattr(ready_env, "smtp_host", None)
        check = _by_key(launch.run_checks(db), "email")
        assert check.state is State.BLOCKED
        assert "stays `pending`" in check.detail

    def test_a_scheduled_pass_that_has_never_run_blocks(
        self, db: Session, ready_env
    ) -> None:
        """The one part of this product that says nothing when it stops."""
        check = _by_key(launch.run_checks(db), "scheduled")
        assert check.state is State.BLOCKED
        assert "never" in check.detail

    def test_a_stale_scheduled_pass_blocks(self, db: Session, ready_env) -> None:
        """**Having run once is not the same as running.**

        A cron service that worked on the day it was created and has been
        erroring since looks identical to a healthy one if the check only asks
        whether a row exists.
        """
        _record_scheduled(db, ago=timedelta(days=3))
        check = _by_key(launch.run_checks(db), "scheduled")
        assert check.state is State.BLOCKED
        assert "hours ago" in check.detail

    def test_a_recent_scheduled_pass_is_ready(self, db: Session, ready_env) -> None:
        _record_scheduled(db, ago=timedelta(minutes=5))
        assert _by_key(launch.run_checks(db), "scheduled").state is State.READY


class TestTheLineThisProjectExistsToHold:
    def test_test_mode_is_never_reported_as_ready(
        self, db: Session, ready_env
    ) -> None:
        """Test mode is the correct posture and not a finished one. Reporting
        it ready would be the checklist saying yes to the question it is least
        able to answer."""
        check = _by_key(launch.run_checks(db), "stripe_mode")
        assert check.state is State.ATTENTION
        assert "TEST mode" in check.detail
        assert "NEW, SEPARATE" in check.remedy

    def test_a_live_key_with_no_declared_entity_blocks(
        self, db: Session, ready_env, monkeypatch
    ) -> None:
        monkeypatch.setattr(ready_env, "stripe_secret_key", "sk_live_abc")
        check = _by_key(launch.run_checks(db), "stripe_mode")
        assert check.state is State.BLOCKED
        assert "STRIPE_PLATFORM_ENTITY" in check.detail

    def test_a_declared_entity_is_reported_as_declared_not_verified(
        self, db: Session, ready_env, monkeypatch
    ) -> None:
        """**The honest answer.** Nothing in this process can know whose EIN a
        Stripe account was opened under, so a declaration is where the check
        stops — and it says so rather than turning green."""
        monkeypatch.setattr(ready_env, "stripe_secret_key", "sk_live_abc")
        monkeypatch.setattr(ready_env, "stripe_platform_entity", "linx LLC")
        check = _by_key(launch.run_checks(db), "stripe_mode")
        assert check.state is State.UNVERIFIABLE, (
            "a declaration nobody can verify was reported as verified"
        )
        assert "linx LLC" in check.detail
        assert "can confirm" in check.detail  # "Nothing in this process can confirm…"

    def test_a_restricted_live_key_counts_as_live(self, ready_env, monkeypatch) -> None:
        """`rk_live_…` moves real money as surely as `sk_live_…`."""
        monkeypatch.setattr(ready_env, "stripe_secret_key", "rk_live_abc")
        assert stripe_client.live_mode() is True

    def test_a_mutating_call_refuses_undeclared_live_mode(
        self, ready_env, monkeypatch
    ) -> None:
        """**Prose does not stop a pasted key; this does.**

        The rule lived only in CLAUDE.md until now. Pasting a live key into a
        service that is already working produces no error and fails no test —
        it just starts routing money through whichever tax identity the account
        happens to belong to.
        """
        monkeypatch.setattr(ready_env, "stripe_secret_key", "sk_live_abc")
        monkeypatch.setattr(ready_env, "stripe_platform_entity", None)

        with pytest.raises(stripe_client.StripeLiveModeUndeclared) as refused:
            stripe_client.post("/refunds", {}, idempotency_key="k")
        assert "STRIPE_PLATFORM_ENTITY" in str(refused.value)

    def test_test_mode_is_untouched_by_the_gate(
        self, ready_env, monkeypatch
    ) -> None:
        """It can only ever refuse to move *real* money. A test key with no
        entity declared is the ordinary state of this repository and must keep
        working exactly as it did.

        Asserted on the gate rather than through `post`, deliberately: going
        through `post` would need a transport, and then a passing test would
        only prove the fake answered — not that this predicate stayed quiet.
        """
        monkeypatch.setattr(ready_env, "stripe_secret_key", "sk_test_abc")
        monkeypatch.setattr(ready_env, "stripe_platform_entity", None)

        assert stripe_client.live_mode() is False
        stripe_client._refuse_undeclared_live_mode()  # does not raise


class TestTheCommandAndTheScreenAgree:
    def test_the_console_reads_the_same_function_the_command_does(
        self, client, admin_user, db: Session
    ) -> None:
        """One answer, two places to see it — the rule the rest of the console
        already follows.

        Deliberately *not* using `ready_env`: that fixture rewrites
        `SECRET_KEY`, which is what signs the JWT, so an already-minted admin
        token stops verifying and the endpoint answers 401. The agreement being
        asserted holds whatever the settings happen to say, so there is nothing
        to set up.
        """
        _record_scheduled(db)
        body = client.get("/api/admin/launch", headers=admin_user["auth"]).json()

        direct = launch.run_checks(db)
        assert [c["key"] for c in body["checks"]] == [c.key for c in direct]
        assert body["blocking_count"] == len(launch.blocking(direct))
        assert body["outstanding_count"] == len(launch.outstanding(direct))
        assert body["launchable"] is launch.is_launchable(direct)

    def test_it_is_admin_only(self, client, make_cleaner) -> None:
        cleaner = make_cleaner(cleared=True)
        assert client.get("/api/admin/launch", headers=cleaner["auth"]).status_code == 403
        assert client.get("/api/admin/launch").status_code == 401

    def test_the_rendered_report_marks_each_state(self, db: Session, ready_env) -> None:
        from app.tasks.launch_check import render

        text = render(launch.run_checks(db))
        assert "[STOP]" in text and "[ask ]" in text
        assert "NOT READY" in text


def _record_scheduled(db: Session, *, ago: timedelta = timedelta(minutes=1)) -> None:
    db.add(
        TaskRun(
            name=launch.SCHEDULED_TASK_NAME,
            finished_at=datetime.now(timezone.utc) - ago,
            duration_ms=120,
            summary="nothing due",
        )
    )
    db.commit()


class TestTheScheduledPassRecordsItself:
    def test_a_pass_writes_one_row_and_keeps_writing_it(self, db: Session) -> None:
        """One row answering one question, not a log nobody prunes."""
        from app.tasks import scheduled
        from sqlalchemy import func, select

        scheduled.run(db)
        scheduled.run(db)

        count = db.execute(
            select(func.count()).select_from(TaskRun).where(
                TaskRun.name == launch.SCHEDULED_TASK_NAME
            )
        ).scalar_one()
        assert count == 1

        row = db.execute(
            select(TaskRun).where(TaskRun.name == launch.SCHEDULED_TASK_NAME)
        ).scalars().one()
        assert row.finished_at is not None
        assert row.summary

    def test_the_check_goes_green_once_a_pass_has_run(
        self, db: Session, ready_env
    ) -> None:
        """The two halves joined up: the pass records, the check reads."""
        from app.tasks import scheduled

        assert _by_key(launch.run_checks(db), "scheduled").state is State.BLOCKED
        scheduled.run(db)
        assert _by_key(launch.run_checks(db), "scheduled").state is State.READY


class TestEvidenceRatherThanMemory:
    def test_a_settled_payment_is_what_proves_the_stripe_path(
        self, db: Session, ready_env, make_cleaner, make_open_turnover, client,
    ) -> None:
        """CLAUDE.md is blunt that no request has ever been made to a real
        Stripe account. A settled row is the evidence that changes, and it is
        worth asking the database rather than trusting a memory of having done
        it."""
        check = _by_key(launch.run_checks(db), "payment_proven")
        assert check.state is State.ATTENTION
        assert "never been verified" in check.detail


class TestALocalRefusalIsNotAnUnknownOutcome:
    """**The gate must not poison the row it refuses.**

    `StripeLiveModeUndeclared` is raised before the request leaves this
    process, so nothing moved and whatever the caller wrote beforehand is still
    exactly true. The handlers keyed on `exc.status`, which is a proxy for
    "Stripe answered" — and a local refusal has no status while being the most
    definite outcome there is.

    Read through that proxy it looked *unknown*, and guardrail 2 treats an
    unknown outcome as permanently unsafe. So the gate meant to protect the
    entity would have quietly made jobs unpayable and money vanish off the
    ledger.
    """

    def test_it_reports_its_outcome_as_known(self) -> None:
        assert stripe_client.StripeLiveModeUndeclared().outcome_known is True
        assert stripe_client.StripeError("could not reach Stripe").outcome_known is False
        assert stripe_client.StripeError("no", status=402).outcome_known is True


class TestTwoPassesAtOnce:
    def test_overlapping_passes_both_record(
        self, db: Session, own_session_per_request
    ) -> None:
        """**The pass promises it is safe to run as often as you like.**

        Select-then-insert breaks that promise at the worst moment: on a first
        deploy there is no row, so two overlapping passes both read `None`,
        both insert, and the unique constraint fails one of them *at commit* —
        after it has done all of its real work.
        """
        from app.db import SessionLocal
        from app.tasks import scheduled

        db.commit()  # release this session's snapshot before the race

        start = threading.Barrier(2)
        errors: list[Exception] = []

        def record(name: str) -> None:
            session = SessionLocal()
            try:
                start.wait(timeout=10)
                scheduled.record_run(
                    session, started=0.0, result={"reminders": 1}
                )
            except Exception as exc:  # noqa: BLE001 - the point of the test
                errors.append(exc)
            finally:
                session.close()

        threads = [
            threading.Thread(target=record, args=("first",)),
            threading.Thread(target=record, args=("second",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            assert not thread.is_alive(), "a pass never returned — deadlock?"

        assert not errors, f"an overlapping pass failed on the bookkeeping: {errors}"

        db.expire_all()
        from sqlalchemy import func

        count = db.execute(
            select(func.count()).select_from(TaskRun).where(
                TaskRun.name == launch.SCHEDULED_TASK_NAME
            )
        ).scalar_one()
        assert count == 1, "still one row answering one question"


class TestTheCheckAsksRatherThanCopies:
    """**A readiness check that re-derives a rule is a second author of it.**

    The console rule from phase 8 applies here for the same reason: a screen
    that disagrees with the alert somebody was sent makes both untrustworthy,
    and there is no way to tell which is lying. These two assert the check is
    reading the owning function rather than a copy that happens to agree today.
    """

    def test_a_deactivated_admin_does_not_count_as_a_recipient(
        self, db: Session, ready_env
    ) -> None:
        """**The check said yes to precisely the failure it exists to catch.**

        `notifications.admins` requires `is_active`; counting `role == ADMIN`
        was half the rule. A database holding only deactivated admins reported
        that somebody receives the alerts while every one of them resolved to
        an empty list.
        """
        from app.core.security import hash_password
        from app.models.user import User

        db.add(
            User(
                email="retired@example.com",
                hashed_password=hash_password("x"),
                full_name="Retired Admin",
                role=UserRole.ADMIN,
                is_active=False,
            )
        )
        db.commit()

        check = _by_key(launch.run_checks(db), "admin")
        assert check.state is State.BLOCKED, (
            "a deactivated admin was counted as somebody who receives alerts"
        )
        assert "active admin" in check.detail

        # And an active one settles it — the same query the sender runs.
        db.add(
            User(
                email="working@example.com",
                hashed_password=hash_password("x"),
                full_name="Working Admin",
                role=UserRole.ADMIN,
            )
        )
        db.commit()
        assert _by_key(launch.run_checks(db), "admin").state is State.READY

    def test_settled_means_what_payments_says_it_means(
        self, db: Session, ready_env, monkeypatch, make_open_turnover
    ) -> None:
        """`payments.settled()` is the single author of "has this been
        collected", and this asserts the check *reads* it.

        **A real payment row is what makes this discriminating.** Patching the
        set and asserting the answer stays `attention` proves nothing against
        an empty table — a hardcoded copy also counts zero and also says
        `attention`, so the test would pass either way. With a row present, a
        copy keeps answering from its own list and never flips.
        """
        from app.models.enums import PaymentStatus
        from app.models.payment import PaymentIn
        from app.services import payments

        job = make_open_turnover()
        db.add(
            PaymentIn(
                turnover_id=uuid.UUID(job["turnover"]["id"]),
                amount_cents=20_000,
                platform_fee_cents=3_000,
                status=PaymentStatus.PENDING,
            )
        )
        db.commit()

        # Nothing has settled: `pending` is not in the real set.
        assert _by_key(launch.run_checks(db), "payment_proven").state is State.ATTENTION

        # Redefine what settled means, and the check must follow.
        monkeypatch.setattr(
            payments, "SETTLED_STATUSES", frozenset({PaymentStatus.PENDING})
        )
        moved = _by_key(launch.run_checks(db), "payment_proven")
        assert moved.state is State.READY, (
            "the check did not follow `payments.SETTLED_STATUSES`, so it is "
            "answering from a copy of the set rather than from its owner"
        )


class TestSetIsNotTheSameAsUsable:
    """**The README's own development value is a truthy string.**

    `PUBLIC_BASE_URL=http://localhost:5173` is what the docs tell you to use
    locally. Carried into a deployment, a truthiness check called it ready
    while `payments._app_base()` used it verbatim for Checkout success and
    cancel URLs and for Express onboarding returns — so an owner finishing a
    payment and a cleaner finishing onboarding both landed on their own
    computer, with the launch panel saying Stripe could send them back.
    """

    @pytest.mark.parametrize(
        "value, why",
        [
            ("http://localhost:5173", "the documented development value"),
            ("https://localhost", "localhost is localhost over TLS too"),
            ("http://127.0.0.1:8000", "loopback by address"),
            ("http://192.168.1.10", "a private network address"),
            ("http://linx.example", "plain HTTP on the public internet"),
            ("linx.example", "not an absolute URL at all"),
            ("", "unset"),
        ],
    )
    def test_an_unusable_return_url_blocks(
        self, db: Session, ready_env, monkeypatch, value, why
    ) -> None:
        monkeypatch.setattr(ready_env, "public_base_url", value)
        check = _by_key(launch.run_checks(db), "public_base_url")
        assert check.state is State.BLOCKED, (
            f"{value!r} was reported as a working return URL ({why})"
        )
        assert check.remedy

    def test_a_real_public_origin_is_ready(
        self, db: Session, ready_env, monkeypatch
    ) -> None:
        monkeypatch.setattr(ready_env, "public_base_url", "https://linx.example")
        assert _by_key(launch.run_checks(db), "public_base_url").state is State.READY

    def test_it_does_not_resolve_dns_to_decide(
        self, db: Session, ready_env, monkeypatch
    ) -> None:
        """**Deliberately not `calendars._refuse_private_address`.**

        That one asks "will our process connect somewhere private" and resolves
        every name to answer it — right for a request this server makes. This
        asks whether a browser can be sent back, which is a question about
        configuration, and a launch check that did DNS would call a deployment
        unready because a new record had not propagated yet.
        """
        import socket

        def explode(*args, **kwargs):  # pragma: no cover - must not be called
            raise AssertionError("the launch check resolved DNS")

        monkeypatch.setattr(socket, "getaddrinfo", explode)
        monkeypatch.setattr(
            ready_env, "public_base_url", "https://not-registered-yet.example"
        )
        assert _by_key(launch.run_checks(db), "public_base_url").state is State.READY


class TestASigningKeyThatIsMerelyDifferent:
    """**Not the development placeholder is not the same as strong.**

    Both the settings validator and this check tested inequality with one
    constant, so `SECRET_KEY=` and `SECRET_KEY=x` passed both: the service
    booted and signed every JWT with a guessable value, and anybody holding an
    ordinary token could forge an admin one. That is the whole of the role
    gate, turned green by a check that was asking a cheaper question than the
    one that mattered.
    """

    @pytest.mark.parametrize("value", ["", "   ", "x", "secret", "changeme"])
    def test_a_weak_key_is_refused(self, value: str) -> None:
        from app.config import weak_secret_key

        assert weak_secret_key(value) is not None

    def test_a_generated_key_passes(self) -> None:
        import secrets

        from app.config import weak_secret_key

        assert weak_secret_key(secrets.token_urlsafe(48)) is None

    @pytest.mark.parametrize("value", ["", "x", "dev-only-insecure-secret-key-change-me"])
    def test_production_refuses_to_boot_on_one(self, value: str) -> None:
        """The launch check reports; the validator is what actually stops it."""
        from app.config import Settings

        with pytest.raises(ValueError, match="SECRET_KEY"):
            Settings(
                environment="production",
                secret_key=value,
                database_url="postgresql://x/y",
            )

    def test_the_check_and_the_validator_ask_the_same_function(
        self, db: Session, ready_env, monkeypatch
    ) -> None:
        """Round two's lesson, one check over: ask the owner of the rule.

        A short key used to turn this green while the validator would have
        refused the same value at boot — two rules about one thing, which is
        how they come apart.
        """
        monkeypatch.setattr(ready_env, "secret_key", "x")
        check = _by_key(launch.run_checks(db), "secret_key")
        assert check.state is State.BLOCKED
        assert "1 characters" in check.detail


class TestSetIsNotTheSameAsDelivering:
    """`SMTP_HOST` being set says a sender will be *constructed*.

    An unreachable host, a refused credential or a sender address the relay
    will not accept all raise at send time, `deliver_pending` writes `failed`,
    and nobody receives anything — while a check titled *Notifications are
    actually sent* reported ready.
    """

    def test_configured_but_nothing_delivered_is_attention(
        self, db: Session, ready_env
    ) -> None:
        check = _by_key(launch.run_checks(db), "email")
        assert check.state is State.ATTENTION, (
            "a host nobody has ever successfully sent through reported ready"
        )
        assert "not the same as reachable" in check.detail

    def test_a_delivered_notification_is_what_turns_it_green(
        self, db: Session, ready_env, admin_user
    ) -> None:
        from app.models.enums import NotificationChannel, NotificationEvent, NotificationStatus
        from app.models.notification import Notification

        db.add(
            Notification(
                recipient_id=admin_user["user"].id,
                destination=admin_user["user"].email,
                event=NotificationEvent.BID_RECEIVED,
                channel=NotificationChannel.EMAIL,
                status=NotificationStatus.SENT,
                dedupe_key=f"test:{uuid.uuid4()}",
                subject="s",
                body="b",
                sent_via=delivery.get_sender().identity,
            )
        )
        db.commit()
        check = _by_key(launch.run_checks(db), "email")
        assert check.state is State.READY
        assert "delivered" in check.detail

    def test_failures_are_named_rather_than_merely_unproven(
        self, db: Session, ready_env, admin_user
    ) -> None:
        """"Configured and failing" and "configured and untried" are different
        problems, and an admin reading the panel needs to tell them apart."""
        from app.models.enums import NotificationChannel, NotificationEvent, NotificationStatus
        from app.models.notification import Notification

        db.add(
            Notification(
                recipient_id=admin_user["user"].id,
                destination=admin_user["user"].email,
                event=NotificationEvent.BID_RECEIVED,
                channel=NotificationChannel.EMAIL,
                status=NotificationStatus.FAILED,
                dedupe_key=f"test:{uuid.uuid4()}",
                subject="s",
                body="b",
            )
        )
        db.commit()
        check = _by_key(launch.run_checks(db), "email")
        assert check.state is State.ATTENTION
        assert "have failed" in check.detail


class TestAnOriginRatherThanAUrl:
    """`payments._app_base()` appends `/turnovers/…` to this value.

    So a query or a fragment does not sit anywhere a path can follow it:
    `https://linx.example#preview` becomes
    `https://linx.example#preview/cleaner/profile`, which is the site root with
    the callback buried in the fragment. The browser arrives somewhere that
    looks almost right, which is worse than arriving nowhere.
    """

    @pytest.mark.parametrize(
        "value",
        [
            "https://linx.example#preview",
            "https://linx.example?tenant=x",
            "https://linx.example/?utm=1",
        ],
    )
    def test_a_query_or_fragment_is_refused(
        self, db: Session, ready_env, monkeypatch, value: str
    ) -> None:
        monkeypatch.setattr(ready_env, "public_base_url", value)
        check = _by_key(launch.run_checks(db), "public_base_url")
        assert check.state is State.BLOCKED, f"{value} reported usable"

    def test_a_path_prefix_is_still_allowed(
        self, db: Session, ready_env, monkeypatch
    ) -> None:
        """A deployment under a sub-path is a real thing, and appending to it
        works — this is about components a path cannot follow, not about
        insisting on a bare host."""
        monkeypatch.setattr(ready_env, "public_base_url", "https://linx.example/app")
        assert _by_key(launch.run_checks(db), "public_base_url").state is State.READY


class TestThePlatformTheAccountsAreOn:
    """The launch *order* is what makes this reachable, so it is a check.

    Step 2 walks a job end to end on the deployed site with test keys, writing
    a test-mode account and an enabled payout flag onto a real cleaner profile.
    Step 5 sets the live key. Nothing about those rows looks wrong afterwards.
    """

    def test_no_connected_accounts_is_ready(self, db: Session, ready_env) -> None:
        check = _by_key(launch.run_checks(db), "stripe_account_modes")
        assert check.state is State.READY

    def test_an_account_from_the_other_platform_blocks(
        self, db: Session, ready_env, make_cleaner, monkeypatch
    ) -> None:
        from app.models.cleaner_profile import CleanerProfile

        cleaner = make_cleaner(cleared=True)
        profile = db.execute(
            select(CleanerProfile).where(
                CleanerProfile.user_id == uuid.UUID(cleaner["user"]["id"])
            )
        ).scalar_one()
        profile.stripe_account_id = "acct_from_test_mode"
        profile.stripe_account_livemode = False
        profile.stripe_platform_id = "acct_platform"
        profile.stripe_payouts_enabled = True
        db.commit()
        monkeypatch.setattr(payments, "platform_account_id", lambda: "acct_platform")

        # Still on the test key it was made under: nothing to say.
        assert (
            _by_key(launch.run_checks(db), "stripe_account_modes").state is State.READY
        )

        monkeypatch.setattr(ready_env, "stripe_secret_key", "sk_live_real")
        check = _by_key(launch.run_checks(db), "stripe_account_modes")
        assert check.state is State.BLOCKED, (
            "the live configuration reported ready while every cleaner held an "
            "account that does not exist on it"
        )
        assert check.remedy

    def test_it_asks_the_function_that_owns_the_rule(self) -> None:
        """Round two's lesson again: the check does not restate the predicate
        as SQL, it asks `payments.connected_account_is_foreign` per row."""
        import inspect

        from app.services import payments

        source = inspect.getsource(launch._stripe_account_modes)
        assert "connected_account_is_foreign" in source
        assert callable(payments.connected_account_is_foreign)


class TestTheModeColumnIsAFloorNotAProof:
    """A boolean cannot tell two same-mode platforms apart.

    `stripe_account_livemode` catches the transition the launch order actually
    prescribes — test mode on the deployed site, then the live key — because
    those differ in mode. Step 4 opens a *new* Connect platform account under
    the new entity, and testing that first means new **test** keys: two
    platforms, one mode, one boolean, compared equal.
    """

    def _with_account(self, db: Session, make_cleaner, platform: str | None) -> None:
        from app.models.cleaner_profile import CleanerProfile

        cleaner = make_cleaner(cleared=True)
        profile = db.execute(
            select(CleanerProfile).where(
                CleanerProfile.user_id == uuid.UUID(cleaner["user"]["id"])
            )
        ).scalar_one()
        profile.stripe_account_id = f"acct_{uuid.uuid4().hex[:12]}"
        profile.stripe_account_livemode = False
        profile.stripe_platform_id = platform
        profile.stripe_payouts_enabled = True
        db.commit()

    def test_a_same_mode_platform_change_blocks(
        self, db: Session, ready_env, make_cleaner, monkeypatch
    ) -> None:
        self._with_account(db, make_cleaner, "acct_the_old_platform")
        monkeypatch.setattr(payments, "platform_account_id", lambda: "acct_the_new_one")

        check = _by_key(launch.run_checks(db), "stripe_account_modes")
        assert check.state is State.BLOCKED, (
            "a same-mode platform change reported ready — the mode booleans "
            "matched and nothing compared the platforms"
        )
        assert check.remedy

    def test_the_same_platform_is_ready(
        self, db: Session, ready_env, make_cleaner, monkeypatch
    ) -> None:
        self._with_account(db, make_cleaner, "acct_platform")
        monkeypatch.setattr(payments, "platform_account_id", lambda: "acct_platform")
        check = _by_key(launch.run_checks(db), "stripe_account_modes")
        assert check.state is State.READY

    def test_an_unresolvable_platform_is_unverifiable_not_ready(
        self, db: Session, ready_env, make_cleaner, monkeypatch
    ) -> None:
        """**The two-state mistake, in the check most able to make it.**

        Stripe unreachable means nothing is known — and an unknown counted as
        a pass is exactly what the fourth state exists to refuse.
        """
        self._with_account(db, make_cleaner, "acct_platform")
        monkeypatch.setattr(payments, "platform_account_id", lambda: None)
        check = _by_key(launch.run_checks(db), "stripe_account_modes")
        assert check.state is State.UNVERIFIABLE
        assert check in launch.outstanding(launch.run_checks(db))

    def test_an_account_from_before_the_platform_was_recorded_is_unverifiable(
        self, db: Session, ready_env, make_cleaner, monkeypatch
    ) -> None:
        """Right mode, unknown platform. Not a pass, because the mode is the
        floor rather than the answer — and not a block, because nothing says
        it is wrong."""
        self._with_account(db, make_cleaner, None)
        monkeypatch.setattr(payments, "platform_account_id", lambda: "acct_platform")
        check = _by_key(launch.run_checks(db), "stripe_account_modes")
        assert check.state is State.UNVERIFIABLE

    def test_no_accounts_asks_stripe_nothing(
        self, db: Session, ready_env, monkeypatch
    ) -> None:
        """Nothing to verify is ready, not unverifiable — and costs no call."""

        def explode():
            raise AssertionError("resolved the platform with nothing to compare")

        monkeypatch.setattr(payments, "platform_account_id", explode)
        assert (
            _by_key(launch.run_checks(db), "stripe_account_modes").state is State.READY
        )

    def test_the_platform_costs_one_network_call_however_many_cleaners(
        self, db: Session, ready_env, make_cleaner, monkeypatch
    ) -> None:
        """N cleaners must not mean N Stripe calls in an admin page load.

        The predicate is asked per row — that is what makes it a single author
        — so the cache in `platform_account_id` is what keeps the *network*
        cost at one. This counts the calls that actually leave the process.
        """
        from app.services import payments as payments_module

        for _ in range(3):
            self._with_account(db, make_cleaner, "acct_platform")

        payments_module._PLATFORM_CACHE.clear()
        calls = []

        def counted(path, **kwargs):
            calls.append(path)
            return {"id": "acct_platform"}

        monkeypatch.setattr(stripe_client, "is_configured", lambda: True)
        monkeypatch.setattr(stripe_client, "get", counted)

        check = _by_key(launch.run_checks(db), "stripe_account_modes")
        assert check.state is State.READY
        assert calls.count("/account") == 1, (
            f"asked Stripe for the platform {calls.count('/account')} times "
            "with three cleaners on the list"
        )
        payments_module._PLATFORM_CACHE.clear()

    def test_a_switched_key_is_not_answered_from_the_old_cache(
        self, ready_env, monkeypatch
    ) -> None:
        """The cache is keyed by the secret key, because the whole failure this
        guards is somebody changing which platform the key points at."""
        from app.services import payments as payments_module

        payments_module._PLATFORM_CACHE.clear()
        monkeypatch.setattr(stripe_client, "is_configured", lambda: True)
        monkeypatch.setattr(stripe_client, "get", lambda p, **kw: {"id": "acct_one"})
        assert payments.platform_account_id() == "acct_one"

        monkeypatch.setattr(ready_env, "stripe_secret_key", "sk_test_a_different_one")
        monkeypatch.setattr(stripe_client, "get", lambda p, **kw: {"id": "acct_two"})
        assert payments.platform_account_id() == "acct_two", (
            "the new key was answered from the previous platform's cache"
        )
        payments_module._PLATFORM_CACHE.clear()

    def test_an_unreachable_stripe_resolves_to_unknown_not_a_mismatch(
        self, ready_env, monkeypatch
    ) -> None:
        """None must read as *not known* — never as a match, and never as a
        mismatch that turns every cleaner unpayable during a Stripe blip."""
        from app.services import payments as payments_module

        payments_module._PLATFORM_CACHE.clear()

        def unreachable(path, **kwargs):
            raise stripe_client.StripeError("could not reach Stripe")

        monkeypatch.setattr(stripe_client, "is_configured", lambda: True)
        monkeypatch.setattr(stripe_client, "get", unreachable)
        assert payments.platform_account_id() is None


class TestAnOutageCostsOneCallNotNPlusOne:
    """The hole in the previous fix, which its own test walked past.

    `platform_account_id` caches a *success* and deliberately not a failure —
    a Stripe blip must not become a permanent "unknown" for the life of the
    process. So the call-counting test passed while measuring only the path
    where the cache does the work: on an outage every call site pays the full
    timeout, and the check asks per cleaner and then once more.
    """

    def _cleaners_with_accounts(self, db: Session, make_cleaner, n: int) -> None:
        from app.models.cleaner_profile import CleanerProfile

        for _ in range(n):
            cleaner = make_cleaner(cleared=True)
            profile = db.execute(
                select(CleanerProfile).where(
                    CleanerProfile.user_id == uuid.UUID(cleaner["user"]["id"])
                )
            ).scalar_one()
            profile.stripe_account_id = f"acct_{uuid.uuid4().hex[:12]}"
            profile.stripe_account_livemode = False
            profile.stripe_platform_id = "acct_platform"
            db.commit()

    def test_an_unreachable_stripe_is_asked_once_not_once_per_cleaner(
        self, db: Session, ready_env, make_cleaner, monkeypatch
    ) -> None:
        """N cleaners must not mean N sequential 30-second timeouts."""
        from app.services import payments as payments_module

        self._cleaners_with_accounts(db, make_cleaner, 3)
        payments_module._PLATFORM_CACHE.clear()

        calls = []

        def unreachable(path, **kwargs):
            calls.append(path)
            raise stripe_client.StripeError("could not reach Stripe")

        monkeypatch.setattr(stripe_client, "is_configured", lambda: True)
        monkeypatch.setattr(stripe_client, "get", unreachable)

        check = _by_key(launch.run_checks(db), "stripe_account_modes")
        assert check.state is State.UNVERIFIABLE
        assert calls.count("/account") == 1, (
            f"an outage cost {calls.count('/account')} sequential Stripe calls "
            "with three cleaners on the list"
        )

    def test_a_failure_is_still_not_cached_permanently(
        self, ready_env, monkeypatch
    ) -> None:
        """Passing the answer down is what bounds the outage, *not* caching the
        failure — a blip must not decide the answer for the whole process."""
        from app.services import payments as payments_module

        payments_module._PLATFORM_CACHE.clear()

        def unreachable(path, **kwargs):
            raise stripe_client.StripeError("could not reach Stripe")

        monkeypatch.setattr(stripe_client, "is_configured", lambda: True)
        monkeypatch.setattr(stripe_client, "get", unreachable)
        assert payments.platform_account_id() is None

        monkeypatch.setattr(stripe_client, "get", lambda p, **kw: {"id": "acct_back"})
        assert payments.platform_account_id() == "acct_back", (
            "the outage was cached, so recovery never took effect"
        )

    def test_a_resolved_unknown_is_not_re_resolved_by_the_predicate(
        self, db: Session, ready_env, make_cleaner, monkeypatch
    ) -> None:
        """`None` passed in is an *answer*, not an absence. Without the
        sentinel the predicate would treat it as "nobody told me" and go
        looking again — which is the N+1 by another route."""
        from app.models.cleaner_profile import CleanerProfile
        from app.services import payments as payments_module

        self._cleaners_with_accounts(db, make_cleaner, 1)
        profile = db.execute(
            select(CleanerProfile).where(
                CleanerProfile.stripe_account_id.is_not(None)
            )
        ).scalars().first()
        payments_module._PLATFORM_CACHE.clear()

        def explode(path, **kwargs):
            raise AssertionError("re-resolved a platform it was handed")

        monkeypatch.setattr(stripe_client, "is_configured", lambda: True)
        monkeypatch.setattr(stripe_client, "get", explode)
        assert not payments.connected_account_is_foreign(profile, platform=None)


class TestEvidenceForTheSenderConfiguredNow:
    """A `SENT` row proves *a* sender worked, not the one configured now.

    Change `SMTP_HOST` to something broken after one successful send and the
    count stays positive — historical success standing as proof about a system
    nobody is using, on the check titled *Notifications are actually sent*.
    """

    def _sent_through(self, db: Session, admin_user, identity: str | None) -> None:
        from app.models.enums import (
            NotificationChannel,
            NotificationEvent,
            NotificationStatus,
        )
        from app.models.notification import Notification

        db.add(
            Notification(
                recipient_id=admin_user["user"].id,
                destination=admin_user["user"].email,
                event=NotificationEvent.BID_RECEIVED,
                channel=NotificationChannel.EMAIL,
                status=NotificationStatus.SENT,
                dedupe_key=f"test:{uuid.uuid4()}",
                subject="s",
                body="b",
                sent_via=identity,
            )
        )
        db.commit()

    def _identity(self) -> str:
        from app.services import delivery

        return delivery.get_sender().identity

    def test_a_send_through_the_current_sender_is_ready(
        self, db: Session, ready_env, admin_user
    ) -> None:
        self._sent_through(db, admin_user, self._identity())
        check = _by_key(launch.run_checks(db), "email")
        assert check.state is State.READY

    def test_a_send_through_a_replaced_sender_is_not(
        self, db: Session, ready_env, admin_user, monkeypatch
    ) -> None:
        self._sent_through(db, admin_user, self._identity())
        assert _by_key(launch.run_checks(db), "email").state is State.READY

        monkeypatch.setattr(ready_env, "smtp_host", "a-different-relay.example")
        check = _by_key(launch.run_checks(db), "email")
        assert check.state is State.ATTENTION, (
            "yesterday's success stood as evidence for a sender replaced since"
        )
        assert "has since been replaced" in check.detail

    def test_a_row_from_before_the_column_is_not_evidence(
        self, db: Session, ready_env, admin_user
    ) -> None:
        """Null is "not evidence for this", which is correct rather than a gap
        — the migration deliberately does not backfill a guess."""
        self._sent_through(db, admin_user, None)
        assert _by_key(launch.run_checks(db), "email").state is State.ATTENTION

    def test_the_identity_never_carries_the_password(self) -> None:
        from app.services import delivery

        sender = delivery.SmtpSender(
            host="relay.example",
            port=587,
            username="user",
            password="hunter2",
            use_tls=True,
            sender="linx@example",
        )
        assert "hunter2" not in sender.identity
        assert "relay.example" in sender.identity


class TestOneSpellingOfTheBaseUrl:
    """`launch` stripped before parsing and `payments._app_base()` did not.

    So `PUBLIC_BASE_URL="https://linx.example "` validated clean and then
    produced a Checkout return URL with a space in the middle of it — the panel
    green while Stripe could not use the value at all.
    """

    def test_the_setting_is_normalised_once_for_every_reader(self) -> None:
        from app.config import Settings

        s = Settings(
            database_url="postgresql://x/y",
            public_base_url="  https://linx.example  ",
        )
        assert s.public_base_url == "https://linx.example"

    def test_whitespace_only_is_unset_rather_than_blank(self) -> None:
        from app.config import Settings

        s = Settings(database_url="postgresql://x/y", public_base_url="   ")
        assert s.public_base_url is None

    def test_the_check_and_the_payment_path_see_the_same_value(
        self, db: Session, ready_env, monkeypatch
    ) -> None:
        """The assertion that would have caught it: compare what the check
        validated against what the URL builder actually uses."""
        from app.services import payments

        monkeypatch.setattr(ready_env, "public_base_url", "https://linx.example")
        assert _by_key(launch.run_checks(db), "public_base_url").state is State.READY
        assert " " not in payments._app_base()
        assert payments._app_base() == "https://linx.example"


class TestTheSenderIdentityCoversEverySetting:
    """Host, port and from-address left three settings out.

    Changing `SMTP_USERNAME`, `SMTP_PASSWORD` or `SMTP_USE_TLS` to something
    that fails kept the identity identical, so a `SENT` row from the old
    credentials went on standing as evidence for the new ones — the staleness
    `sent_via` exists to stop, one setting over.
    """

    def _sender(self, **overrides):
        from app.services import delivery

        kwargs = dict(
            host="relay.example",
            port=587,
            username="user",
            password="hunter2",
            use_tls=True,
            sender="linx@example",
        )
        kwargs.update(overrides)
        return delivery.SmtpSender(**kwargs)

    @pytest.mark.parametrize(
        "change",
        [
            {"username": "somebody-else"},
            {"password": "a-rotated-one"},
            {"use_tls": False},
            {"port": 2525},
            {"host": "another-relay.example"},
            {"sender": "someone@example"},
        ],
    )
    def test_every_delivery_affecting_setting_changes_it(self, change: dict) -> None:
        assert self._sender().identity != self._sender(**change).identity, (
            f"changing {list(change)[0]} left the identity unchanged, so old "
            "evidence would stand for the new configuration"
        )

    def test_the_same_configuration_is_stable(self) -> None:
        """It has to be, or every deploy would invalidate its own evidence."""
        assert self._sender().identity == self._sender().identity

    def test_no_secret_appears_in_it(self) -> None:
        """Written to every delivered row and read back onto an admin screen —
        a password on a screen is a password in a screenshot."""
        identity = self._sender().identity
        assert "hunter2" not in identity
        assert "user" not in identity.replace("linx@example", "")

    def test_the_fingerprint_is_not_an_offline_password_oracle(self) -> None:
        """**Salting is not enough here.**

        `sent_via` is stored in the database and rendered on an admin screen,
        and the host and port are printed beside the digest while usernames are
        guessable — so a fast hash salted with public values stops a
        precomputed table and nothing else. Anybody who could read the column
        held a verifier for guessing the SMTP password, which is an exposure
        the field itself created.

        Keyed under `SECRET_KEY`, the digest is uncomputable without the
        application secret, so this asserts the secret actually participates.
        """
        import hashlib

        from app.config import settings

        identity = self._sender().identity
        assert hashlib.sha256(b"hunter2").hexdigest()[:12] not in identity

        original = settings.secret_key
        try:
            settings.secret_key = "a-completely-different-application-secret"
            assert self._sender().identity != identity, (
                "the secret does not participate, so the digest is computable "
                "by anyone holding the public fields"
            )
        finally:
            settings.secret_key = original

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ({"username": "a|b", "password": "c"}, {"username": "a", "password": "b|c"}),
            ({"username": "a", "password": ""}, {"username": "", "password": "a"}),
            ({"host": "a.example", "sender": "b@example"},
             {"host": "a.example\u0000b@example", "sender": ""}),
        ],
    )
    def test_the_fields_cannot_be_confused_with_each_other(
        self, left: dict, right: dict
    ) -> None:
        """Joining on a delimiter is not injective: two different, perfectly
        valid configurations serialise to the same bytes, so switching between
        them would leave old evidence standing."""
        assert self._sender(**left).identity != self._sender(**right).identity
