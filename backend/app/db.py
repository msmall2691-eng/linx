"""Database engine and session plumbing."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
    future=True,
)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session.

    The session is rolled back and closed on the way out; routes commit
    explicitly. Guardrail 1 work (phase 4) opens its own transaction on this
    same session and must keep the lock and the writes inside it.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
