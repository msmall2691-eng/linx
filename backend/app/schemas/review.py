"""Response shapes for reviews.

**The shape is part of the mechanism.** `ReviewOut` carries no hint of whether
the other side has written — no "waiting on them", no count of pending reviews,
no timestamp that could be differenced. `ReviewsOut.awaiting_counterpart` is
deliberately absent for the same reason: knowing they have written is knowing to
hurry, and knowing they have not is knowing you can still go first.

What the author is told about their own is a different question, and the answer
is on `ReviewsOut.mine`: you may always read what you wrote.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import UserRole


class ReviewIn(BaseModel):
    """One side's review. Written once, never edited.

    A review you can rewrite after the other side's appears is a review you can
    rewrite *in response to* it, which is the behaviour the delay exists to
    prevent — so there is no update shape here, only this one.
    """

    rating: int = Field(ge=1, le=5)
    #: Optional. A star with no words is still a review; forcing prose produces
    #: filler rather than signal.
    text: str | None = Field(default=None, max_length=4000)


class ReviewOut(BaseModel):
    """A review somebody can read.

    Only ever built from a review that is visible, or from the reader's own.
    Nothing in this shape distinguishes the two, because nothing needs to.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    turnover_id: uuid.UUID
    #: Which side wrote it — owner or cleaner. Not *who*: the owner's identity
    #: stays withheld from cleaners everywhere else, and a review is not the
    #: place it leaks.
    author_role: UserRole
    rating: int
    text: str | None
    created_at: datetime
    #: When it became readable. Null only on the reader's own unrevealed review.
    visible_at: datetime | None


class ReputationOut(BaseModel):
    """What somebody's visible reviews add up to.

    Display only — rating-weighted ranking is out of scope for v1, and the board
    still sorts by urgency so a cleaner with no reviews is not buried.
    """

    model_config = ConfigDict(from_attributes=True)

    count: int
    average: float | None


class ReviewsOut(BaseModel):
    """Everything one person may see about a turnover's reviews.

    **One shape per resource**: every endpoint that answers about a turnover's
    reviews answers with this, submit included, so a screen that replaces its
    state with an action's reply keeps everything it was showing.
    """

    turnover_id: uuid.UUID
    #: Whether this person may still write one. False once they have, and false
    #: for anybody who was not part of the turnover.
    can_review: bool
    #: Why not, in words, or null when they can. Rendered verbatim.
    blocker: str | None
    #: The reader's own review, revealed or not. Null if they have not written.
    mine: ReviewOut | None
    #: Everything visible to everyone. Excludes `mine` while `mine` is hidden.
    visible: list[ReviewOut]
