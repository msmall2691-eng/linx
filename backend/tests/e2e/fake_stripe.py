"""A stand-in for Stripe that speaks over real HTTP.

The browser test it serves is the carry-over row that has been on the list since
day one: *a real click-through of bid → award → payment, not just "the button
renders"*. Faking `stripe_client.post` would not satisfy that — it would skip the
form encoding, the client, and the redirect, which is most of what can actually
be wrong between a button and a charge.

So this is a real HTTP server, and the app talks to it exactly the way it would
talk to Stripe: the same client, the same bracketed form bodies, the same
redirect out to a hosted page and back, and a **real signed webhook** posted
back to the app afterwards, which the app verifies with the same HMAC it would
use in production. Only the far end is a fake.

What it deliberately does not do is pretend to be Stripe's behaviour in any
depth — no card logic, no 3DS, no failures. It answers the handful of calls this
codebase makes, records what it was asked, and is honest about being a fake.
Walking one payment through a real test-mode key before launch is still on the
list, and this does not replace it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import socket
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class FakeStripeServer:
    """Serves Stripe's API surface and its hosted pages on one port."""

    def __init__(self, *, webhook_secret: str) -> None:
        self.webhook_secret = webhook_secret
        self.app_base_url: str | None = None
        self.calls: list[tuple[str, dict]] = []
        self.sessions: dict[str, dict] = {}
        #: Flipped once the cleaner walks through the onboarding page, so the
        #: test can assert that payout readiness is Stripe's answer rather than
        #: something the app assumed after a redirect.
        self.payouts_enabled = False

        self._port = self._free_port()
        self._server = ThreadingHTTPServer(("127.0.0.1", self._port), _make_handler(self))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    @property
    def api_base(self) -> str:
        return f"{self.base_url}/v1"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def body_for(self, path: str) -> dict:
        for called, body in self.calls:
            if called == path:
                return body
        raise AssertionError(f"the app never called {path}; it called {[c for c, _ in self.calls]}")

    def send_webhook(self, kind: str, obj: dict) -> None:
        """Post a properly signed delivery, the way Stripe would.

        Signed for real: the app verifies it with the same HMAC it uses in
        production, so a broken signature check fails this test rather than
        surfacing the first time a real payment lands.
        """
        assert self.app_base_url, "the app's URL was never handed to the fake"
        payload = json.dumps({"type": kind, "data": {"object": obj}}).encode()
        timestamp = int(__import__("time").time())
        signature = hmac.new(
            self.webhook_secret.encode(),
            f"{timestamp}.".encode() + payload,
            hashlib.sha256,
        ).hexdigest()

        request = urllib.request.Request(
            f"{self.app_base_url}/api/stripe/webhook",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": f"t={timestamp},v1={signature}",
            },
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            assert response.status == 200, f"the app refused the webhook: {response.status}"


def _unflatten(form: dict[str, list[str]]) -> dict:
    """Turn Stripe's bracketed form keys back into something assertable.

    `payment_intent_data[transfer_data][destination]` becomes nested dicts, so
    a test can assert the destination charge's shape on what actually went over
    the wire rather than on what the caller meant to send.
    """
    out: dict = {}
    for key, values in form.items():
        parts = key.replace("]", "").split("[")
        cursor = out
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = values[0]
    return out


def _make_handler(fake: FakeStripeServer):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # noqa: D102 - quiet in test output
            pass

        def _json(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, markup: str) -> None:
            body = markup.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # -- the API -----------------------------------------------------
        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode() if length else ""
            form = _unflatten(parse_qs(raw)) if raw else {}
            fake.calls.append((path, form))

            if path == "/v1/accounts":
                return self._json({"id": "acct_e2e", "type": "express"})

            if path == "/v1/account_links":
                return self._json({"url": f"{fake.base_url}/onboarding"})

            if path == "/v1/checkout/sessions":
                session_id = f"cs_e2e_{len(fake.sessions) + 1}"
                fake.sessions[session_id] = form
                return self._json(
                    {
                        "id": session_id,
                        "url": f"{fake.base_url}/pay/{session_id}",
                        "payment_intent": f"pi_{session_id}",
                    }
                )

            if path == "/v1/refunds":
                return self._json({"id": "re_e2e", "status": "succeeded"})

            if path.startswith("/pay/") and path.endswith("/confirm"):
                return self._confirm(path.split("/")[2])

            return self._json({"error": {"message": f"fake stripe: no {path}"}}, 404)

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            fake.calls.append((path, {}))

            if path.startswith("/v1/accounts/"):
                return self._json(
                    {
                        "id": path.rsplit("/", 1)[-1],
                        "details_submitted": True,
                        "payouts_enabled": fake.payouts_enabled,
                    }
                )

            if path == "/onboarding":
                # Stripe's hosted Express form, compressed to the one thing the
                # test needs from it: the cleaner finishes, and afterwards the
                # *account* says payouts are enabled — not the redirect.
                fake.payouts_enabled = True
                return self._html(
                    "<h1>Stripe onboarding (fake)</h1>"
                    f'<a id="done" href="{fake.app_base_url}/cleaner/profile?stripe=return">'
                    "Finish</a>"
                )

            if path.startswith("/pay/"):
                session_id = path.split("/")[2]
                session = fake.sessions.get(session_id, {})
                total = (
                    session.get("line_items", {})
                    .get("0", {})
                    .get("price_data", {})
                    .get("unit_amount", "?")
                )
                return self._html(
                    "<h1>Stripe Checkout (fake)</h1>"
                    f"<p>Amount: {total}</p>"
                    f'<form method="post" action="/pay/{session_id}/confirm">'
                    '<button id="pay" type="submit">Pay</button></form>'
                )

            return self._json({"error": {"message": f"fake stripe: no {path}"}}, 404)

        # -- the hosted payment page -------------------------------------
        def _confirm(self, session_id: str) -> None:
            session = fake.sessions[session_id]
            # Stripe sends the webhook; the browser is redirected back. Both
            # happen here, in that order, the way they do in production.
            fake.send_webhook(
                "checkout.session.completed",
                {
                    "object": "checkout.session",
                    "id": session_id,
                    "payment_status": "paid",
                    "payment_intent": f"pi_{session_id}",
                    "metadata": session.get("metadata", {}),
                },
            )
            self.send_response(303)
            self.send_header("Location", session["success_url"])
            self.end_headers()

    return Handler
