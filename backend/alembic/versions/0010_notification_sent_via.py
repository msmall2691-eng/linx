"""notifications.sent_via — which sender configuration delivered it

A `SENT` row proves that *a* sender worked. It does not say which one, and the
launch check was reading it as evidence that the currently configured sender
works — so changing `SMTP_HOST`, the port or the from-address to something
broken left yesterday's success standing as proof about today's configuration,
on the one check whose title is *Notifications are actually sent*.

Evidence that is not tied to what it is evidence *for* is not evidence. This
records host, port and from-address on each delivered row — the three that
decide whether a relay accepts a message — and never the password, because
this is stored on every delivered row and read back onto a screen.

Null on rows written before this existed, and on anything not actually
delivered. Not backfilled: there is no way to know what sent them, and writing
a guess is how the check would go on lying with a column to point at.

Revision ID: 0010_notification_sent_via
Revises: 0009_stripe_account_mode
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_notification_sent_via"
down_revision: str | None = "0009_stripe_account_mode"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "notifications",
        sa.Column("sent_via", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("notifications", "sent_via")
