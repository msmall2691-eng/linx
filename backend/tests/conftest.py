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
from sqlalchemy import text  # noqa: E402
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
