"""Test fixtures.

**Tests run against a real PostgreSQL database, not SQLite.** This is not
fussiness. Guardrail 1 (see CLAUDE.md) depends on `SELECT ... FOR UPDATE`, which
SQLite does not implement — a suite running on SQLite would go green while the
double-award race stayed wide open in production. The same goes for the native
enum types, the `can_take_jobs` generated column, and the deferred constraint
behavior this schema relies on.

Point `TEST_DATABASE_URL` at a throwaway database. Locally:

    createdb linx_test
    TEST_DATABASE_URL=postgresql+psycopg://linx:linx@localhost:5432/linx_test pytest
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://linx:linx@localhost:5432/linx_test"

# Set before anything imports app.config, whose Settings object is cached at
# import time. Importing the app first would bind it to the development
# database and quietly run the suite against real data.
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)
os.environ["ENVIRONMENT"] = "test"
os.environ.setdefault("SECRET_KEY", "test-only-secret-key")

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db import SessionLocal, engine, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> Iterator[None]:
    """Build the schema by running the real migrations.

    Not `Base.metadata.create_all` — that would test the models against
    themselves and never touch the migration that production actually runs.
    A migration that does not match the models fails here instead of at deploy.
    """
    alembic_cfg = Config(os.path.join(BACKEND_DIR, "alembic.ini"))
    alembic_cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))

    command.downgrade(alembic_cfg, "base")
    command.upgrade(alembic_cfg, "head")
    yield
    engine.dispose()


@pytest.fixture(autouse=True)
def clean_process_caches() -> Iterator[None]:
    """Process-global caches are state too, and they outlive TRUNCATE.

    `payments._PLATFORM_CACHE` remembers which Connect platform a secret key
    resolved to. Every test shares one fake key, so without this a test that
    resolves a platform decides the answer for every test after it — and the
    suite passes or fails on ordering, which is the kind of test failure that
    gets rerun rather than read.
    """
    from app.services import payments

    payments._PLATFORM_CACHE.clear()
    yield
    payments._PLATFORM_CACHE.clear()


@pytest.fixture(autouse=True)
def clean_tables() -> Iterator[None]:
    """Empty every table between tests, without rebuilding the schema."""
    yield
    table_names = ", ".join(f'"{t}"' for t in Base.metadata.tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {table_names} RESTART IDENTITY CASCADE"))


@pytest.fixture
def db() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(db: Session) -> Iterator[TestClient]:
    """A client whose requests share the test's session, so a test can read
    back what a request wrote without a second connection."""

    def _get_db_override() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_db] = _get_db_override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def own_session_per_request() -> Iterator[None]:
    """Give every request its own session, as production does.

    The `client` fixture deliberately shares the test's session so a test can
    read back what a request wrote. That sharing makes a concurrency test
    meaningless — two requests on one connection cannot race — so any test that
    means to prove a row lock swaps in the real thing for its duration.

    Lives here rather than in one test module because there are now two locked
    paths worth racing: accepting a bid (`test_awards.py`) and writing a review
    (`test_reviews.py`).
    """

    def _get_db() -> Iterator[Session]:
        session = SessionLocal()
        try:
            yield session
        finally:
            session.close()

    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = _get_db
    try:
        yield
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous


@pytest.fixture
def make_user(client: TestClient):
    """Sign a user up through the real endpoint and hand back the payload."""

    def _make_user(
        role: str = "owner",
        email: str | None = None,
        password: str = "correct-horse-battery",
        full_name: str = "Test Person",
    ) -> dict:
        email = email or f"{role}-{os.urandom(4).hex()}@example.com"
        resp = client.post(
            "/api/auth/signup",
            json={
                "email": email,
                "password": password,
                "full_name": full_name,
                "role": role,
            },
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        data["password"] = password
        data["auth"] = {"Authorization": f"Bearer {data['access_token']}"}
        return data

    return _make_user


@pytest.fixture
def admin_user(db: Session):
    """An admin, created directly — signup deliberately refuses to make one."""
    from app.core.security import create_access_token, hash_password
    from app.models import User, UserRole

    user = User(
        email="admin@example.com",
        hashed_password=hash_password("correct-horse-battery"),
        full_name="Admin Person",
        role=UserRole.ADMIN,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    token = create_access_token(subject=user.id, role=user.role.value)
    return {
        "user": user,
        "access_token": token,
        "auth": {"Authorization": f"Bearer {token}"},
        "password": "correct-horse-battery",
    }


PORTLAND_ME = (43.6591, -70.2568)


@pytest.fixture(autouse=True)
def isolated_document_storage(tmp_path, monkeypatch):
    """Point uploads at a per-test directory.

    Without this the suite writes into the real storage directory and tests
    read each other's files — and a test that deletes a document would reach
    outside its own fixture data.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "document_storage_dir", str(tmp_path / "documents"))
    yield


@pytest.fixture
def make_cleaner(client: TestClient, make_user, db: Session):
    """Sign a cleaner up, give them a profile, and optionally clear them.

    Clearing goes through the real vetting statuses rather than writing
    `can_take_jobs`: that column is generated by Postgres and cannot be set, by
    design. A fixture that could fake it would be testing something the
    application cannot do.
    """

    def _make_cleaner(
        *,
        cleared: bool = False,
        lat: float | None = PORTLAND_ME[0],
        lng: float | None = PORTLAND_ME[1],
        radius_miles: int = 25,
        has_insurance: bool = False,
    ) -> dict:
        import uuid as _uuid

        from app.models import CleanerProfile, VerificationStatus

        cleaner = make_user(role="cleaner")
        resp = client.put(
            "/api/cleaner/profile",
            json={
                "bio": "Ten years of turnovers.",
                "service_lat": str(lat) if lat is not None else None,
                "service_lng": str(lng) if lng is not None else None,
                "service_radius_miles": radius_miles,
            },
            headers=cleaner["auth"],
        )
        assert resp.status_code == 200, resp.text

        profile = db.execute(
            select(CleanerProfile).where(
                CleanerProfile.user_id == _uuid.UUID(cleaner["user"]["id"])
            )
        ).scalar_one()

        if cleared:
            profile.id_verification_status = VerificationStatus.APPROVED
            profile.background_check_status = VerificationStatus.APPROVED
        if has_insurance:
            profile.has_insurance_on_file = True
        if cleared or has_insurance:
            db.commit()
            db.refresh(profile)

        cleaner["profile_id"] = str(profile.id)
        cleaner["profile"] = profile
        return cleaner

    return _make_cleaner


@pytest.fixture
def make_open_turnover(client: TestClient, make_user):
    """An owner with a property and an open turnover, at given coordinates."""

    def _make_open_turnover(
        *,
        lat: float = PORTLAND_ME[0],
        lng: float = PORTLAND_ME[1],
        owner: dict | None = None,
        nickname: str = "Seaside Cottage",
        days_out: int = 5,
        checkin_hours_after: int | None = 96,
        budget_cents: int | None = 14_500,
    ) -> dict:
        from datetime import datetime, timedelta, timezone

        owner = owner or make_user(role="owner")
        prop = client.post(
            "/api/properties",
            json={
                "nickname": nickname,
                "address_line1": "1 Harbor Way",
                "city": "Portland",
                "state": "ME",
                "postal_code": "04101",
                "lat": str(lat),
                "lng": str(lng),
                "bedrooms": 2,
                "bathrooms": "1.5",
                "access_notes": "Lockbox on the rail, code 4417.",
                "cleaning_notes": "Linens in the hall closet.",
            },
            headers=owner["auth"],
        )
        assert prop.status_code == 201, prop.text

        checkout = datetime.now(timezone.utc) + timedelta(days=days_out)
        payload = {
            "property_id": prop.json()["id"],
            "checkout_at": checkout.isoformat(),
            "owner_budget_cents": budget_cents,
            "notes": "Guests had a dog.",
        }
        if checkin_hours_after is not None:
            payload["checkin_at"] = (
                checkout + timedelta(hours=checkin_hours_after)
            ).isoformat()

        turnover = client.post("/api/turnovers", json=payload, headers=owner["auth"])
        assert turnover.status_code == 201, turnover.text

        return {
            "owner": owner,
            "property": prop.json(),
            "turnover": turnover.json(),
        }

    return _make_open_turnover
