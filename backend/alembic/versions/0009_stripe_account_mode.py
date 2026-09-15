"""cleaner_profiles.stripe_account_livemode — which platform the account is on

Stripe objects are scoped to the mode of the key that created them. An
`acct_…` made with a test key does not exist to a live key, and the id itself
does not say which one it is. Until now this database recorded a connected
account, whether payouts were enabled, and whether onboarding was finished —
three facts about *some* platform, with nothing saying which.

That is only latent while the platform never changes, and phase 9's own launch
order changes it on purpose:

    2. Walk one job end to end on the deployed site …
    5. Set the live key **and** `STRIPE_PLATFORM_ENTITY` together.

Step 2 writes a test-mode `acct_…` onto a real cleaner profile, with
`stripe_payouts_enabled = true` because the test-mode account really was
enabled. Step 5 swaps the platform. `ensure_connected_account` then returns the
stored id because it is non-empty, `payout_blocker` finds nothing missing
because the flags are true, and the first live destination charge names an
account that does not exist — the money is collected from the owner and the
transfer has nowhere to go. Nothing about the row looks wrong.

So the mode is recorded rather than inferred from the id. NULL means the row
predates this column and its mode is genuinely unknown, which is treated the
same as a mismatch: an unknown is never assumed to be the good outcome.

Deliberately **not** backfilled. A migration cannot read which key created an
account, and guessing "these were all test mode" would be exactly the
assumption this column exists to stop — it would be written down as a fact and
believed by every reader afterwards.

Revision ID: 0009_stripe_account_mode
Revises: 0008_task_runs
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_stripe_account_mode"
down_revision: str | None = "0008_task_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "cleaner_profiles",
        sa.Column("stripe_account_livemode", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("cleaner_profiles", "stripe_account_livemode")
