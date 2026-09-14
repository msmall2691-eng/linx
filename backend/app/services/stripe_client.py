"""The only door to Stripe.

**Every Stripe call in this codebase goes through this module.** No route, no
task, no webhook handler talks to Stripe directly. That is guardrail 2's "one
helper, one door" rule, and the reason for it is narrow and concrete: the moment
there are two places that can charge a card, one of them is missing an
idempotency key, and the way you find out is a customer saying they were billed
twice.

So the key is not optional here. `post()` refuses a mutating request that
carries neither an `idempotency_key` nor an explicit `non_idempotent_reason`
saying why this call cannot duplicate anything that matters. Exactly one call
signs that second form, and it moves no money. A caller who has nothing stable
to derive a key from has a design problem, not a parameter problem.

Three more things this module is deliberate about:

**No key configured means the payment path is disabled, not faked.** Without
`STRIPE_SECRET_KEY` every call raises `StripeNotConfigured` and the endpoints
above answer with a reason. That is a different posture from Checkr (manual
fallback) and SMTP (log instead of send) on purpose: a human can run a
background check by hand and a log line can stand in for an email, but nothing
stands in for money. A row must never say it collected something it did not.

**Written to Stripe's documented REST interface, not verified against it.**
Same honesty as the Checkr client: the request shapes and response handling are
covered by tests against a mocked transport, but no request has been made to a
real Stripe account from this codebase. Walk one payment through with a test-mode
key before relying on it. Test mode is fully sandboxed — no EIN, no SSN, no real
money — which is exactly why that walkthrough is cheap and there is no excuse
for skipping it.

**Test mode until the go-live gate.** Going live means a new, separate Connect
platform account under the new entity. Never a migrated one, and never real
pilot transactions under a personal SSN or another company's EIN.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger("linx.stripe")

REQUEST_TIMEOUT_SECONDS = 30.0

#: How much clock skew a webhook signature may carry before it is refused.
#: Stripe's own recommendation; a replayed delivery from last week is not a
#: notification, it is an attack.
WEBHOOK_TOLERANCE_SECONDS = 300


class StripeError(Exception):
    """Stripe refused the request, or could not be reached."""

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


class StripeNotConfigured(StripeError):
    """No API key. The payment path is off, and says so."""

    def __init__(self) -> None:
        super().__init__(
            "Payments are not configured on this deployment. "
            "Set STRIPE_SECRET_KEY (test mode) to enable them."
        )


class WebhookVerificationError(Exception):
    """The delivery did not carry a valid signature for our secret."""


def is_configured() -> bool:
    """Whether money can move at all. Read by endpoints before they promise."""
    return bool(settings.stripe_secret_key)


# --------------------------------------------------------------------------
# Form encoding
#
# Stripe's API takes form-encoded bodies with bracketed keys for nested values
# (`transfer_data[destination]`), not JSON. Encoding is split out and tested on
# its own because a silently mis-encoded nested key does not error — it is
# ignored, and a destination charge quietly becomes a plain one that keeps the
# cleaner's money on the platform.
# --------------------------------------------------------------------------


def encode_form(data: dict[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    """Flatten a nested dict into Stripe's bracketed form pairs."""
    pairs: list[tuple[str, str]] = []
    for key, value in data.items():
        field = f"{prefix}[{key}]" if prefix else str(key)
        if value is None:
            continue
        if isinstance(value, dict):
            pairs.extend(encode_form(value, field))
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                if isinstance(item, dict):
                    pairs.extend(encode_form(item, f"{field}[{index}]"))
                else:
                    pairs.append((f"{field}[{index}]", _scalar(item)))
        else:
            pairs.append((field, _scalar(value)))
    return pairs


def _scalar(value: Any) -> str:
    # Stripe wants lowercase booleans, and integers as-is. Money is already
    # integer cents by the time it reaches here; nothing in this file turns a
    # number into a float.
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


# --------------------------------------------------------------------------
# The call itself
# --------------------------------------------------------------------------


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=settings.stripe_api_base.rstrip("/"),
        headers={
            "Authorization": f"Bearer {settings.stripe_secret_key}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )


def post(
    path: str,
    data: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    non_idempotent_reason: str | None = None,
    stripe_account: str | None = None,
) -> dict[str, Any]:
    """Make a mutating Stripe call.

    `idempotency_key` must be derived from an identifier already in the
    database — an award id plus a constant naming the operation. Never a fresh
    value per attempt: a genuine retry has to produce the *same* key to be
    recognized as a retry rather than a second charge, and a `uuid4()` per
    attempt does not add safety, it removes the only mechanism there was.

    A call with no key is possible and deliberately awkward: it requires
    `non_idempotent_reason`, a sentence saying why this particular call cannot
    duplicate anything that matters. Exactly one call in this codebase uses it
    (an Express onboarding link, which expires and must be re-issued, and which
    moves no money). The point of the parameter is that skipping the key is a
    signed decision somebody can grep for, not an omission nobody notices.
    """
    if not is_configured():
        raise StripeNotConfigured()
    if not idempotency_key and not non_idempotent_reason:
        raise ValueError(
            "a mutating Stripe call needs a derived idempotency key, or an "
            "explicit non_idempotent_reason saying why it cannot have one"
        )

    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    if stripe_account:
        # Acting on behalf of a connected account.
        headers["Stripe-Account"] = stripe_account

    try:
        with _client() as client:
            response = client.post(path, content=_urlencode(data), headers=headers)
    except httpx.HTTPError as exc:
        # The outcome is genuinely unknown here: the request may have reached
        # Stripe and succeeded. The caller has already written its attempt, so
        # the row lands in requires_review rather than being retried blindly.
        raise StripeError(f"could not reach Stripe: {exc}") from exc

    return _read(response)


def get(path: str, *, stripe_account: str | None = None) -> dict[str, Any]:
    """Read something back. No idempotency key: reads do not create."""
    if not is_configured():
        raise StripeNotConfigured()

    headers = {"Stripe-Account": stripe_account} if stripe_account else {}
    try:
        with _client() as client:
            response = client.get(path, headers=headers)
    except httpx.HTTPError as exc:
        raise StripeError(f"could not reach Stripe: {exc}") from exc

    return _read(response)


def _urlencode(data: dict[str, Any]) -> str:
    from urllib.parse import urlencode

    return urlencode(encode_form(data))


def _read(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise StripeError(
            f"Stripe returned a non-JSON response ({response.status_code})"
        ) from exc

    if response.status_code >= 400:
        error = payload.get("error") or {}
        raise StripeError(
            error.get("message") or f"Stripe refused the request ({response.status_code})",
            code=error.get("code") or error.get("type"),
            status=response.status_code,
        )
    return payload


# --------------------------------------------------------------------------
# Webhooks
# --------------------------------------------------------------------------


def verify_webhook(payload: bytes, signature_header: str | None, *, now: float | None = None) -> dict:
    """Check a delivery really came from Stripe, then parse it.

    An unsigned POST that says a payment succeeded is a POST from anyone. With
    no `STRIPE_WEBHOOK_SECRET` configured this refuses every delivery rather
    than trusting one — the same reasoning as the payment path being disabled
    rather than faked without a key.
    """
    import json

    secret = settings.stripe_webhook_secret
    if not secret:
        raise WebhookVerificationError(
            "STRIPE_WEBHOOK_SECRET is not set; webhook deliveries are refused."
        )
    if not signature_header:
        raise WebhookVerificationError("no Stripe-Signature header")

    timestamp, signatures = _parse_signature_header(signature_header)
    if timestamp is None or not signatures:
        raise WebhookVerificationError("malformed Stripe-Signature header")

    reference = now if now is not None else time.time()
    if abs(reference - timestamp) > WEBHOOK_TOLERANCE_SECONDS:
        raise WebhookVerificationError("signature timestamp is outside the tolerance window")

    expected = hmac.new(
        secret.encode(), f"{int(timestamp)}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    # compare_digest against every v1 signature: Stripe sends more than one
    # while a secret is being rotated.
    if not any(hmac.compare_digest(expected, candidate) for candidate in signatures):
        raise WebhookVerificationError("signature does not match")

    try:
        return json.loads(payload)
    except ValueError as exc:
        raise WebhookVerificationError("delivery body is not JSON") from exc


def _parse_signature_header(header: str) -> tuple[float | None, list[str]]:
    timestamp: float | None = None
    signatures: list[str] = []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            try:
                timestamp = float(value)
            except ValueError:
                return None, []
        elif key == "v1":
            signatures.append(value)
    return timestamp, signatures
