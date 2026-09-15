"""Sending a notification — the transport, and nothing else.

Split from `notifications.py` on purpose. That module decides *what happened,
who hears about it, and what they are told*; this one only knows how to hand a
subject and a body to an address. Swapping SMTP for a hosted API later is a
change here and nowhere else.

**No SMTP host configured means logged, not broken.** The same posture as
background checks without a Checkr key: the product works end to end before
there is a mail provider, and the notification row exists either way, so "did
anyone tell them" has an answer from day one. What it must never do is look
like a successful send when nothing left the building — the logging sender says
plainly, in the row and in the log, that it was not delivered.

A sender either returns (delivered) or raises `DeliveryError` (it did not).
There is deliberately no third answer: a silent partial success is the thing
that leaves a person wondering whether they were told.
"""

from __future__ import annotations

import hashlib
import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

from app.config import settings

logger = logging.getLogger("linx.delivery")


class DeliveryError(Exception):
    """The message did not go out, and we know it did not."""


@dataclass(frozen=True)
class Outgoing:
    destination: str
    subject: str
    body: str


class Sender:
    """A transport. Return means delivered; raise means it was not."""

    #: True when the message actually left the building. The logging sender
    #: sets this False so nothing downstream can mistake a development log line
    #: for a delivered email.
    delivers: bool = True

    @property
    def identity(self) -> str:
        """Which configuration this is, recorded on what it delivers.

        A `SENT` row proves *a* sender worked; without this it does not say
        which, so changing `SMTP_HOST` to something broken left yesterday's
        success standing as evidence for today's configuration. The launch
        check reads it, which is the whole point — evidence that is not tied to
        what it is evidence *for* is not evidence.

        The password is deliberately not in it. This is stored on every
        delivered row and read back onto a screen.
        """
        return "unknown"

    def send(self, message: Outgoing) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class LoggingSender(Sender):
    """The launch posture: record it, log it, do not pretend it was sent."""

    delivers = False

    @property
    def identity(self) -> str:
        return "log"

    def send(self, message: Outgoing) -> None:
        logger.info(
            "notification not delivered (no SMTP_HOST configured) -> %s: %s\n%s",
            message.destination,
            message.subject,
            message.body,
        )


class SmtpSender(Sender):
    """Plain SMTP. One connection per message, which is right at this volume.

    Every failure mode of `smtplib` becomes `DeliveryError`, so a caller cannot
    accidentally treat a refused recipient or a dropped connection as success.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str | None,
        password: str | None,
        use_tls: bool,
        sender: str,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_tls = use_tls
        self.sender = sender

    @property
    def identity(self) -> str:
        """**Every setting that decides whether a delivery succeeds.**

        The first version was host, port and from-address, which left three
        out: changing `SMTP_USERNAME`, `SMTP_PASSWORD` or `SMTP_USE_TLS` to
        something that fails kept the identity identical, so a `SENT` row from
        the old credentials went on standing as evidence for the new ones —
        the same staleness this field exists to stop, one setting over.

        The readable half names the host so the launch check can say which
        relay it means. The credentials go through a digest instead: this is
        written to every delivered row and read back onto an admin screen, and
        a password on a screen is a password in a screenshot.

        It is **salted with the host, port and username** rather than being a
        bare hash of the password, so the digest cannot be matched against a
        precomputed table — the same reasoning as `source_feed_key` being a
        digest because the feed URL is a credential. Truncated because what is
        needed is "did this change", not a cryptographic commitment.
        """
        secrets_fingerprint = hashlib.sha256(
            "|".join(
                [
                    self.host,
                    str(self.port),
                    self.username or "",
                    self.password or "",
                    "tls" if self.use_tls else "plain",
                ]
            ).encode()
        ).hexdigest()[:12]
        return f"{self.host}:{self.port} as {self.sender} #{secrets_fingerprint}"

    def send(self, message: Outgoing) -> None:
        email = EmailMessage()
        email["From"] = self.sender
        email["To"] = message.destination
        email["Subject"] = message.subject
        email.set_content(message.body)

        try:
            with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
                if self.use_tls:
                    smtp.starttls()
                if self.username and self.password:
                    smtp.login(self.username, self.password)
                smtp.send_message(email)
        except (OSError, smtplib.SMTPException) as exc:
            raise DeliveryError(f"{type(exc).__name__}: {exc}") from exc


def get_sender() -> Sender:
    """The configured transport, or the logging one when there is none."""
    if not settings.smtp_host:
        return LoggingSender()
    return SmtpSender(
        host=settings.smtp_host,
        port=settings.smtp_port,
        username=settings.smtp_username,
        password=settings.smtp_password,
        use_tls=settings.smtp_use_tls,
        sender=settings.email_from,
    )
