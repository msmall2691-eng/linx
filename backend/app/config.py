"""Application settings, read from the environment.

Everything the app needs to run in production comes from environment
variables. Defaults here are development-only and deliberately unusable in
production: `SECRET_KEY` has no safe default, and startup refuses to continue
with the placeholder value when `ENVIRONMENT` is not `development`.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_SECRET_KEY = "dev-only-insecure-secret-key-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["development", "test", "production"] = "development"

    # Railway injects DATABASE_URL for its Postgres add-on.
    database_url: str = "postgresql+psycopg://linx:linx@localhost:5432/linx_dev"

    secret_key: str = DEV_SECRET_KEY
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 12

    # Single region at launch — no multi-region logic anywhere (see CLAUDE.md).
    region_name: str = "Greater Portland, ME"
    #: IANA timezone for that region. "Same day" and every date a person reads
    #: are answered in local time; a 4pm checkout and a 10pm checkin are one day
    #: in Portland and two in UTC.
    region_timezone: str = "America/New_York"

    # Comma-separated list, or "*" in development.
    cors_origins: str = "http://localhost:5173"

    # Directory holding the built frontend. Mounted at "/" when it exists.
    frontend_dist: str = "static"

    # Vetting document uploads. On Railway this directory must be a mounted
    # volume — a container filesystem is replaced on every deploy, so without
    # one the uploaded IDs disappear while their rows survive.
    document_storage_dir: str = "var/documents"
    #: Upload ceiling. A phone photo of an ID is comfortably under this.
    max_document_bytes: int = 15 * 1024 * 1024

    # Background checks (phase 3). Without a key the manual provider is used
    # and an admin records the outcome by hand — see
    # app/services/background_check.py.
    checkr_api_key: str | None = None
    checkr_api_base: str = "https://api.checkr.com/v1"
    #: Checkr package slug to order. Varies by account.
    checkr_package: str = "tasker_standard"

    # Notifications (phase 5). Without SMTP_HOST the logging sender is used and
    # every notification is recorded and written to the log instead of sent —
    # the same launch posture as background checks without a Checkr key. The
    # row still exists either way, so "did anyone tell them" has an answer
    # before there is a mail provider.
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_use_tls: bool = True
    #: From address on everything the product sends.
    email_from: str = "linx <no-reply@linx.local>"

    #: How far ahead of checkout the day-of reminder goes out, and how close to
    #: checkout an unclaimed turnover becomes an alert. Two separate numbers on
    #: purpose: one is a courtesy, the other is an operational alarm, and the
    #: alarm's cutoff is deliberately *not* the urgency ladder (see CLAUDE.md).
    reminder_hours_before: int = Field(default=24, ge=1, le=168)
    unclaimed_alert_hours_before: int = Field(default=24, ge=1, le=168)

    # ---------------------------------------------------------------------
    # Stripe (phase 6). TEST MODE ONLY until the go-live gate.
    #
    # Without STRIPE_SECRET_KEY the payment path is *disabled*, not faked: the
    # endpoints refuse with a clear reason and no row claims to have collected
    # anything. That differs on purpose from the Checkr and SMTP postures — a
    # manual fallback makes sense for a background check and for an email, and
    # makes no sense at all for money.
    # ---------------------------------------------------------------------
    stripe_secret_key: str | None = None
    stripe_api_base: str = "https://api.stripe.com/v1"
    #: Verifies the signature on incoming webhooks. Without it the webhook
    #: endpoint refuses every delivery rather than trusting an unsigned POST
    #: that says a payment succeeded.
    stripe_webhook_secret: str | None = None
    #: Where Stripe sends the owner back after hosted Checkout, and where
    #: Express onboarding returns to. Empty means "work it out from the request",
    #: which is right in development and wrong behind a proxy in production.
    public_base_url: str | None = None

    # ---------------------------------------------------------------------
    # Reviews (phase 7)
    #
    # How long a one-sided review waits before it is revealed anyway. The
    # delay is the whole mechanism: a review that appears the moment it lands
    # rewards getting in first with a bad one to poison the other side's, so
    # neither is visible until both are in — or until this window passes and
    # the silent side has had every chance.
    #
    # Fourteen days is long enough that "I was busy" is not a reason it went
    # unanswered, and short enough that an honest review is still useful to the
    # next person reading it.
    # ---------------------------------------------------------------------
    review_reveal_after_days: int = Field(default=14, ge=1, le=90)

    #: Google Places / Geocoding key. **Optional, and the product works without
    #: it**: address autocomplete is the precise path, and the region's own
    #: town table (`app/services/places.py`) is the fallback that keeps every
    #: property on the map. Same posture as Checkr and SMTP — a missing key
    #: costs precision, not function — and deliberately unlike Stripe, where
    #: nothing stands in for money.
    google_maps_api_key: str | None = None

    platform_fee_bps: int = Field(
        default=1500,
        ge=0,
        le=10_000,
        description="Platform cut in basis points. Read by phase 6, stored now.",
    )

    @field_validator("database_url")
    @classmethod
    def normalize_database_url(cls, v: str) -> str:
        """Accept the `postgres://` URL Railway hands out.

        SQLAlchemy 2.0 dropped the bare `postgres://` scheme, and we pin the
        psycopg 3 driver explicitly rather than letting SQLAlchemy pick psycopg2.
        """
        if v.startswith("postgres://"):
            v = v.replace("postgres://", "postgresql://", 1)
        if v.startswith("postgresql://"):
            v = v.replace("postgresql://", "postgresql+psycopg://", 1)
        return v

    @model_validator(mode="after")
    def reject_dev_secret_in_production(self) -> "Settings":
        if self.environment == "production" and self.secret_key == DEV_SECRET_KEY:
            raise ValueError(
                "SECRET_KEY must be set to a real value when ENVIRONMENT=production"
            )
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
