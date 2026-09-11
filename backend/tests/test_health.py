from __future__ import annotations

from fastapi.testclient import TestClient


def test_health_is_live(client: TestClient) -> None:
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_health_db_reaches_postgres(client: TestClient) -> None:
    resp = client.get("/api/health/db")
    assert resp.status_code == 200
    assert resp.json()["database"] == "ok"


def test_public_config_exposes_the_one_region(client: TestClient) -> None:
    resp = client.get("/api/config")
    assert resp.status_code == 200
    # One region at launch — a name to display, never a selection.
    assert resp.json()["region_name"]
