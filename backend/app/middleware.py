"""Request-level guards that have to run before anything reads the body.

**Why this is ASGI middleware and not a check in a route.** With FastAPI's
`UploadFile`, the multipart body is consumed and spooled *before* the endpoint
runs — and before its authentication dependency runs with it. A size check
inside the handler therefore measures something that has already arrived and
already cost disk and bandwidth, and it measures it for callers who were never
signed in. `storage.save` streams its writes and aborts on overrun, which is
right and still too late: by then Starlette has written the upload to a
temporary file of its own.

So the limit lives here, outside the router, where the bytes can be refused as
they arrive.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class _BodyTooLarge(Exception):
    """Raised out of `receive` to stop a request that is still arriving."""


class BodySizeLimit:
    """Refuse a request body over `max_bytes`, before it is read.

    Two halves, because either alone is a gap:

    * **The declared length**, which costs nothing to check and stops every
      honest client at the first byte.
    * **The bytes actually received**, because `Content-Length` is
      client-supplied — a chunked request omits it entirely, which is precisely
      what somebody sending an enormous body on purpose would do.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = _declared_length(scope)
        if declared is not None and declared > self.max_bytes:
            await _refuse(send, self.max_bytes)
            return

        received = 0
        started = False

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _BodyTooLarge
            return message

        async def watching_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, watching_send)
        except _BodyTooLarge:
            # Nothing to say if the app already answered; otherwise this is the
            # only response the client will get.
            if not started:
                await _refuse(send, self.max_bytes)


def _declared_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers") or []:
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


async def _refuse(send: Send, limit: int) -> None:
    body = (
        b'{"detail":"That request is larger than this server accepts '
        b'(%d bytes)."}' % limit
    )
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
