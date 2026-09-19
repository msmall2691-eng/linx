"""Shapes for the message thread on a booked job.

**No user id and no email on the way out.** A message carries who it is *from
the reader's point of view* — see `messages.visible_sender` — because the
cleaner is never told whose house it is, and a thread is the easiest place in
this product to leak that. An id the frontend could look up would be the same
leak with an extra step.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class MessageIn(BaseModel):
    """Something to say about this job."""

    body: str = Field(min_length=1, max_length=4000)

    @field_validator("body")
    @classmethod
    def must_say_something(cls, v: str) -> str:
        """**Stripped here, so a blank message is a 422 rather than a 409.**

        `min_length` passes a body of three spaces, and the service refuses it
        — correctly, but as a *conflict*, which is the code for "the state
        will not allow this" rather than "that is not a message". The service
        check stays as the backstop it should have been all along: this shape
        is not the only way a message can be written.
        """
        text = v.strip()
        if not text:
            raise ValueError("There is nothing to send.")
        return text


class MessageOut(BaseModel):
    """One message, labelled for whoever asked for it."""

    id: uuid.UUID
    #: Who sent it, as this reader should see them: "You", "The owner", or the
    #: cleaner's name. Resolved server-side so a second screen cannot invent
    #: its own answer and widen the privacy boundary by accident.
    sender_label: str
    #: Whether the reader wrote it, so the screen can align it without having
    #: to compare names — which would be a string comparison standing in for an
    #: identity check.
    mine: bool
    body: str
    sent_at: datetime


class ThreadOut(BaseModel):
    """The conversation on one booking.

    `can_send` is carried rather than inferred, because the conditions that
    close a thread — the booking ended, the job is finished — live in
    `messages.py` and a screen re-deriving them would eventually disagree with
    the endpoint that refuses.
    """

    award_id: uuid.UUID
    can_send: bool
    #: Why not, when `can_send` is false. Null when it is true.
    closed_reason: str | None
    messages: list[MessageOut]
