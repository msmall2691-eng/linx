"""Background checks: the result mapping, and the Checkr client's shape.

The mapping is where the judgment lives, so it is tested directly. The HTTP
client is tested against a mocked transport — which proves the request shape
and the response handling, and does **not** prove the integration works against
a real Checkr account. Nothing here has ever talked to Checkr.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from app.models.enums import VerificationStatus
from app.services.background_check import (
    BackgroundCheckError,
    CheckrBackgroundCheck,
    ManualBackgroundCheck,
    get_provider,
    interpret_checkr_report,
)


class TestTheResultMapping:
    def test_a_clear_report_approves(self) -> None:
        outcome = interpret_checkr_report(
            {"status": "complete", "result": "clear"}, provider_ref="cand_1"
        )
        assert outcome.status is VerificationStatus.APPROVED

    def test_a_consider_report_is_never_an_automatic_rejection(self) -> None:
        """`consider` means a human has to weigh what surfaced.

        Auto-declining on it would skip the adverse-action process the FCRA
        requires, so it parks in `pending` with an instruction instead. This is
        the single most important assertion in this file.
        """
        outcome = interpret_checkr_report(
            {"status": "complete", "result": "consider"}, provider_ref="cand_1"
        )
        assert outcome.status is not VerificationStatus.REJECTED
        assert outcome.status is VerificationStatus.PENDING
        assert "adverse-action" in outcome.note

    @pytest.mark.parametrize(
        "report",
        [
            {"status": "pending"},
            {"status": "dispute", "result": "consider"},
            {"status": "suspended"},
            {},
            {"status": "something_new", "result": "???"},
        ],
    )
    def test_everything_uncertain_stays_pending(self, report: dict) -> None:
        """An unknown vendor state must never read as a clearance."""
        outcome = interpret_checkr_report(report, provider_ref="cand_1")
        assert outcome.status is VerificationStatus.PENDING

    def test_the_reference_is_carried_through(self) -> None:
        outcome = interpret_checkr_report(
            {"status": "complete", "result": "clear"}, provider_ref="cand_42"
        )
        assert outcome.provider_ref == "cand_42"


class TestTheManualProvider:
    def test_requesting_parks_it_for_a_human(self) -> None:
        """The launch posture: vetting is manual, and that is not a stub."""
        outcome = ManualBackgroundCheck().request(user=None, profile=None)
        assert outcome.status is VerificationStatus.PENDING
        assert "admin" in outcome.note

    def test_it_is_the_default_when_no_key_is_configured(self, monkeypatch) -> None:
        from app.config import settings

        monkeypatch.setattr(settings, "checkr_api_key", None)
        assert isinstance(get_provider(), ManualBackgroundCheck)

    def test_checkr_is_used_when_a_key_is_present(self, monkeypatch) -> None:
        from app.config import settings

        monkeypatch.setattr(settings, "checkr_api_key", "sk_test_not_real")
        assert isinstance(get_provider(), CheckrBackgroundCheck)


class TestTheCheckrClient:
    """Request shape and response handling, against a mocked transport.

    Not evidence that the live integration works — see the module docstring.
    """

    def _provider_with(self, handler, monkeypatch) -> CheckrBackgroundCheck:
        provider = CheckrBackgroundCheck("sk_test_not_real", base_url="https://checkr.test")
        transport = httpx.MockTransport(handler)

        def _client(self=provider):
            token = base64.b64encode(b"sk_test_not_real:").decode()
            return httpx.Client(
                base_url="https://checkr.test",
                headers={"Authorization": f"Basic {token}"},
                transport=transport,
            )

        monkeypatch.setattr(provider, "_client", _client)
        return provider

    def test_requesting_creates_a_candidate_then_an_invitation(
        self, monkeypatch
    ) -> None:
        """The invitation is what keeps SSN and date of birth out of our database.

        The candidate gives those to Checkr directly, so this request carries
        only the contact details we already hold.
        """
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content) if request.content else {}
            seen.append((request.url.path, body))
            if request.url.path == "/candidates":
                return httpx.Response(201, json={"id": "cand_abc"})
            return httpx.Response(201, json={"id": "inv_xyz", "status": "pending"})

        provider = self._provider_with(handler, monkeypatch)

        class _User:
            full_name = "Dana Rivers"
            email = "dana@example.com"

        outcome = provider.request(user=_User(), profile=object())

        assert [path for path, _ in seen] == ["/candidates", "/invitations"]
        candidate_body = seen[0][1]
        assert candidate_body == {
            "first_name": "Dana",
            "last_name": "Rivers",
            "email": "dana@example.com",
        }
        # Nothing sensitive is sent, because we never hold it.
        assert "ssn" not in candidate_body
        assert "dob" not in candidate_body
        assert "date_of_birth" not in candidate_body

        assert seen[1][1]["candidate_id"] == "cand_abc"
        assert outcome.status is VerificationStatus.PENDING
        assert outcome.provider_ref == "cand_abc"

    def test_the_api_key_goes_in_basic_auth_with_an_empty_password(
        self, monkeypatch
    ) -> None:
        headers = {}

        def handler(request: httpx.Request) -> httpx.Response:
            headers.update(request.headers)
            if request.url.path == "/candidates":
                return httpx.Response(201, json={"id": "cand_abc"})
            return httpx.Response(201, json={"id": "inv_xyz"})

        provider = self._provider_with(handler, monkeypatch)

        class _User:
            full_name = "Dana Rivers"
            email = "dana@example.com"

        provider.request(user=_User(), profile=object())

        scheme, _, token = headers["authorization"].partition(" ")
        assert scheme == "Basic"
        assert base64.b64decode(token).decode() == "sk_test_not_real:"

    def test_a_vendor_error_surfaces_as_a_background_check_error(
        self, monkeypatch
    ) -> None:
        """Never swallowed: a failed order must not look like a pending check."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "bad key"})

        provider = self._provider_with(handler, monkeypatch)

        class _User:
            full_name = "Dana Rivers"
            email = "dana@example.com"

        with pytest.raises(BackgroundCheckError):
            provider.request(user=_User(), profile=object())

    def test_refresh_reads_the_candidates_report(self, monkeypatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/reports"
            assert request.url.params["candidate_id"] == "cand_abc"
            return httpx.Response(
                200, json={"data": [{"status": "complete", "result": "clear"}]}
            )

        provider = self._provider_with(handler, monkeypatch)
        outcome = provider.refresh(provider_ref="cand_abc")
        assert outcome.status is VerificationStatus.APPROVED

    def test_no_report_yet_is_pending_not_an_error(self, monkeypatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": []})

        provider = self._provider_with(handler, monkeypatch)
        outcome = provider.refresh(provider_ref="cand_abc")
        assert outcome.status is VerificationStatus.PENDING
        assert "invitation" in outcome.note


class TestTheEndpoints:
    def test_a_cleaner_orders_their_own_check(self, client, make_cleaner) -> None:
        cleaner = make_cleaner()
        resp = client.post("/api/cleaner/background-check", headers=cleaner["auth"])
        assert resp.status_code == 200
        assert resp.json()["background_check_status"] == "pending"
        assert resp.json()["can_take_jobs"] is False

    def test_ordering_twice_does_not_order_twice(self, client, make_cleaner) -> None:
        """A second order while one is outstanding bills twice and muddles the queue."""
        cleaner = make_cleaner()
        client.post("/api/cleaner/background-check", headers=cleaner["auth"])
        resp = client.post("/api/cleaner/background-check", headers=cleaner["auth"])
        assert resp.status_code == 200
        assert resp.json()["background_check_status"] == "pending"

    def test_refresh_needs_a_vendor_reference(
        self, client, make_cleaner, admin_user
    ) -> None:
        cleaner = make_cleaner()
        resp = client.post(
            f"/api/admin/cleaners/{cleaner['profile_id']}/background-check/refresh",
            headers=admin_user["auth"],
        )
        assert resp.status_code == 409

    def test_a_later_poll_never_demotes_an_approved_cleaner(
        self, client, make_cleaner, admin_user, db, monkeypatch
    ) -> None:
        """A cleared cleaner losing clearance to a transient vendor blip would
        silently pull them off every job they had bid on."""
        import uuid as _uuid

        from app.models import CleanerProfile
        from app.services import background_check as bc

        cleaner = make_cleaner(cleared=True)
        profile = db.get(CleanerProfile, _uuid.UUID(cleaner["profile_id"]))
        profile.background_check_provider_ref = "cand_abc"
        db.commit()

        class _Flaky:
            def request(self, **_):
                raise AssertionError("not called")

            def refresh(self, *, provider_ref):
                return bc.CheckOutcome(
                    status=VerificationStatus.PENDING, provider_ref=provider_ref
                )

        from app.main import app

        app.dependency_overrides[bc.get_provider] = lambda: _Flaky()
        try:
            resp = client.post(
                f"/api/admin/cleaners/{cleaner['profile_id']}/background-check/refresh",
                headers=admin_user["auth"],
            )
        finally:
            app.dependency_overrides.pop(bc.get_provider, None)

        assert resp.status_code == 200
        assert resp.json()["background_check_status"] == "approved"
        assert resp.json()["can_take_jobs"] is True
