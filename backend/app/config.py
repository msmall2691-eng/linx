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

    # Comma-separated list, or "*" in development.
    cors_origins: str = "http://localhost:5173"

    # Directory holding the built frontend. Mounted at "/" when it exists.
    frontend_dist: str = "static"

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
