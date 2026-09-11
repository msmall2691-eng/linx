"""Liveness and readiness."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness — does not touch the database."""
    return {"status": "ok", "environment": settings.environment}


@router.get("/health/db")
def health_db(db: Session = Depends(get_db)) -> dict[str, str]:
    """Readiness — confirms the database actually answers."""
    db.execute(text("SELECT 1"))
    return {"status": "ok", "database": "ok"}
