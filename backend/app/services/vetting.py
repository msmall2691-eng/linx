"""The one place that decides whether a cleaner may bid.

`can_take_jobs` is computed by Postgres from the two vetting statuses, so the
answer itself has a single author already (see `app.models.cleaner_profile`).
What this module adds is the *other* half of that guarantee: one function that
turns the answer into a reason, so the bidding gate and anything that explains
itself to a cleaner cannot drift apart.

The trap this avoids is specific and real: a badge that says "verified" beside a
bid button that silently refuses, with no way to tell which one is lying. Every
caller — the gate, the profile response, the board — asks here.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.cleaner_profile import CleanerProfile
from app.models.enums import VerificationStatus


@dataclass(frozen=True)
class VettingState:
    """Why a cleaner can or cannot bid, in a form a person can read."""

    can_take_jobs: bool
    #: Short, ordered list of what is still outstanding. Empty when cleared.
    blockers: tuple[str, ...]
    #: Things worth showing prominently that do **not** block bidding.
    warnings: tuple[str, ...]

    @property
    def summary(self) -> str:
        if self.can_take_jobs:
            return "You're cleared to bid on turnovers."
        return "You can't bid yet: " + "; ".join(self.blockers)


def _describe(status: VerificationStatus, noun: str) -> str | None:
    if status is VerificationStatus.APPROVED:
        return None
    if status is VerificationStatus.NOT_STARTED:
        return f"{noun} not started"
    if status is VerificationStatus.PENDING:
        return f"{noun} under review"
    return f"{noun} was rejected"


def evaluate(profile: CleanerProfile | None) -> VettingState:
    """Read the profile's vetting state and say what it means.

    Reads `can_take_jobs` rather than recomputing it. The database owns that
    boolean; re-deriving it here would put a second author on the rule and
    reintroduce exactly the disagreement the generated column prevents.
    """
    if profile is None:
        return VettingState(
            can_take_jobs=False,
            blockers=("your cleaner profile isn't set up yet",),
            warnings=(),
        )

    blockers = [
        description
        for description in (
            _describe(profile.id_verification_status, "ID verification"),
            _describe(profile.background_check_status, "background check"),
        )
        if description is not None
    ]

    warnings = []
    # A flag, not a gate. Requiring a COI while supply is scarce kills the
    # launch, so a missing one is shown prominently instead of blocking.
    if not profile.has_insurance_on_file:
        warnings.append("no proof of insurance on file")
    if profile.service_lat is None or profile.service_lng is None:
        # Not a vetting blocker, but nothing will appear on the board without
        # it, which is worth saying out loud rather than looking broken.
        warnings.append("no service area set, so no turnovers will show up")

    return VettingState(
        can_take_jobs=bool(profile.can_take_jobs),
        blockers=tuple(blockers),
        warnings=tuple(warnings),
    )


def note_document_uploaded(profile: CleanerProfile, document_type) -> None:
    """Move ID verification into review when supporting evidence arrives.

    Uploading is what puts a cleaner in front of a human. An ID or a reference
    lands them in the admin queue by moving the status out of `not_started`
    (or out of `rejected`, since re-uploading after a rejection is a re-appeal,
    not a permanent bar). Insurance does not touch it: a COI is a flag at v1,
    never a gate.

    It deliberately never sets `approved`. Only an admin does that.
    """
    from app.models.enums import DocumentType

    if document_type is DocumentType.INSURANCE:
        return
    if profile.id_verification_status is not VerificationStatus.APPROVED:
        profile.id_verification_status = VerificationStatus.PENDING


def apply_document_review(profile: CleanerProfile, document, status) -> None:
    """Reflect a reviewed document on the profile.

    An approved insurance certificate is the only document that changes the
    profile by itself, because it sets a flag rather than a gate. Approving an
    ID does **not** clear the cleaner: the spec asks a human to weigh a photo ID
    *and* a reference together, so the identity decision is its own explicit
    admin action rather than a side effect of ticking off one file.
    """
    from app.models.enums import DocumentStatus, DocumentType

    if document.type is DocumentType.INSURANCE:
        profile.has_insurance_on_file = status is DocumentStatus.APPROVED
