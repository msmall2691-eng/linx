"""Ordering a criminal background check, and reading the result.

A photo ID confirms identity, not history. For someone entering a stranger's
home unsupervised that gap is the whole point of vetting, so this is a real
check through a vendor built for it rather than a box an admin ticks.

Three decisions are encoded here deliberately:

**Checkr collects the sensitive data, not us.** The flow creates a candidate
with the contact details we already have, then sends that candidate an
*invitation*. The candidate gives Checkr their SSN and date of birth directly.
Those never touch this database, which is the same reasoning behind using
Stripe Express for payouts: the less regulated data we hold, the less there is
to lose.

**A "consider" result is never an automatic rejection.** Checkr returns
`clear` or `consider`; `consider` means something surfaced that a human has to
weigh, and acting adversely on it has a legally defined process (the FCRA
adverse-action sequence). So `consider` parks the check in `pending` with a
note for the admin queue and refuses to guess. Auto-rejecting on `consider`
would be both wrong and unlawful.

**No key configured means manual, not broken.** With no `CHECKR_API_KEY` the
manual provider is used and an admin records the outcome by hand. That is the
launch posture in CLAUDE.md, and it means the vetting flow works end to end
before a Checkr account exists.
"""

from __future__ import annotations

import base64
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

from app.config import settings
from app.models.cleaner_profile import CleanerProfile
from app.models.enums import VerificationStatus
from app.models.user import User

REQUEST_TIMEOUT_SECONDS = 20.0


class BackgroundCheckError(Exception):
    """The vendor could not be reached, or refused the request."""


@dataclass(frozen=True)
class CheckOutcome:
    """What a provider knows about a check right now."""

    status: VerificationStatus
    #: The vendor's identifier, stored on the profile so the check can be
    #: followed up without guessing which report belongs to whom.
    provider_ref: str | None = None
    #: Shown in the admin queue. Carries the "why" for a pending or rejected
    #: check; never contains report contents.
    note: str | None = None


class BackgroundCheckProvider(ABC):
    @abstractmethod
    def request(self, *, user: User, profile: CleanerProfile) -> CheckOutcome:
        """Start a check for this cleaner."""

    @abstractmethod
    def refresh(self, *, provider_ref: str) -> CheckOutcome:
        """Read the current state of a previously requested check."""


class ManualBackgroundCheck(BackgroundCheckProvider):
    """No vendor configured: a human runs and records the check.

    This is the launch posture, not a stub. The spec is explicit that vetting
    is manual at v1 and should not be automated away, so the flow is complete:
    requesting moves the check to `pending`, it lands in the admin queue, and
    an admin approves or rejects it there.
    """

    def request(self, *, user: User, profile: CleanerProfile) -> CheckOutcome:
        return CheckOutcome(
            status=VerificationStatus.PENDING,
            provider_ref=None,
            note="Awaiting a manual background check by an admin.",
        )

    def refresh(self, *, provider_ref: str) -> CheckOutcome:
        # Nothing to poll; an admin moves this one.
        return CheckOutcome(status=VerificationStatus.PENDING, provider_ref=provider_ref)


class CheckrBackgroundCheck(BackgroundCheckProvider):
    """Checkr, via the candidate + invitation flow.

    **Not verified against the live Checkr API.** It is written to Checkr's
    documented interface and its response handling is covered by tests against
    a mocked transport, but no request has been made to a real Checkr account
    from this codebase. Point it at a Checkr sandbox key and walk one candidate
    through before relying on it.
    """

    def __init__(self, api_key: str, *, base_url: str | None = None, package: str | None = None):
        self.api_key = api_key
        self.base_url = (base_url or settings.checkr_api_base).rstrip("/")
        self.package = package or settings.checkr_package

    def _client(self) -> httpx.Client:
        # Checkr uses HTTP Basic with the API key as the username and an empty
        # password. Built explicitly so the empty password is visible rather
        # than looking like an omission.
        token = base64.b64encode(f"{self.api_key}:".encode()).decode()
        return httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Basic {token}"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

    def request(self, *, user: User, profile: CleanerProfile) -> CheckOutcome:
        first_name, _, last_name = user.full_name.strip().partition(" ")
        try:
            with self._client() as client:
                candidate = client.post(
                    "/candidates",
                    json={
                        "first_name": first_name or user.full_name.strip(),
                        "last_name": last_name or "",
                        "email": user.email,
                    },
                )
                candidate.raise_for_status()
                candidate_id = candidate.json()["id"]

                # The invitation is what keeps SSN and date of birth out of
                # this database: the candidate hands them to Checkr directly.
                invitation = client.post(
                    "/invitations",
                    json={"candidate_id": candidate_id, "package": self.package},
                )
                invitation.raise_for_status()
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise BackgroundCheckError(f"could not order a Checkr check: {exc}") from exc

        return CheckOutcome(
            status=VerificationStatus.PENDING,
            provider_ref=candidate_id,
            note="Checkr invitation sent; waiting for the cleaner to complete it.",
        )

    def refresh(self, *, provider_ref: str) -> CheckOutcome:
        try:
            with self._client() as client:
                response = client.get("/reports", params={"candidate_id": provider_ref})
                response.raise_for_status()
                reports = response.json().get("data") or []
        except (httpx.HTTPError, ValueError) as exc:
            raise BackgroundCheckError(f"could not read the Checkr report: {exc}") from exc

        if not reports:
            return CheckOutcome(
                status=VerificationStatus.PENDING,
                provider_ref=provider_ref,
                note="Checkr has no report yet; the invitation may be outstanding.",
            )

        return interpret_checkr_report(reports[0], provider_ref=provider_ref)


def interpret_checkr_report(report: dict, *, provider_ref: str) -> CheckOutcome:
    """Map a Checkr report onto our vetting status.

    Split out from the HTTP client so the mapping — the part with the judgment
    in it — is testable without a network call.
    """
    status = (report.get("status") or "").lower()
    result = (report.get("result") or "").lower()

    if status in {"pending", "", "dispute"}:
        return CheckOutcome(
            status=VerificationStatus.PENDING,
            provider_ref=provider_ref,
            note="Checkr report is still in progress.",
        )

    if status == "suspended":
        # Checkr stopped: usually missing information from the candidate.
        return CheckOutcome(
            status=VerificationStatus.PENDING,
            provider_ref=provider_ref,
            note="Checkr suspended the report — it usually needs more information from the cleaner.",
        )

    if status == "complete" and result == "clear":
        return CheckOutcome(
            status=VerificationStatus.APPROVED,
            provider_ref=provider_ref,
            note="Checkr returned clear.",
        )

    if status == "complete" and result == "consider":
        # Deliberately NOT a rejection. "Consider" means a human has to weigh
        # what surfaced, and acting adversely has a defined legal process.
        return CheckOutcome(
            status=VerificationStatus.PENDING,
            provider_ref=provider_ref,
            note=(
                "Checkr returned 'consider'. An admin must review the report and follow "
                "the adverse-action process before rejecting — do not auto-decline."
            ),
        )

    return CheckOutcome(
        status=VerificationStatus.PENDING,
        provider_ref=provider_ref,
        note=f"Unrecognised Checkr state (status={status!r}, result={result!r}); needs a human.",
    )


def get_provider() -> BackgroundCheckProvider:
    """FastAPI dependency: Checkr when configured, otherwise manual."""
    if settings.checkr_api_key:
        return CheckrBackgroundCheck(settings.checkr_api_key)
    return ManualBackgroundCheck()
