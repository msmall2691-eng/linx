"""Signup, login, identity, and the role gate."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.mark.parametrize("role", ["owner", "cleaner"])
def test_signup_returns_a_usable_token(client: TestClient, role: str) -> None:
    resp = client.post(
        "/api/auth/signup",
        json={
            "email": f"new-{role}@example.com",
            "password": "correct-horse-battery",
            "full_name": "New Person",
            "role": role,
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["user"]["role"] == role
    assert "password" not in body["user"]
    assert "hashed_password" not in body["user"]

    me = client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me.status_code == 200
    assert me.json()["email"] == f"new-{role}@example.com"


def test_signup_normalizes_email_case(client: TestClient) -> None:
    client.post(
        "/api/auth/signup",
        json={
            "email": "MixedCase@Example.COM",
            "password": "correct-horse-battery",
            "full_name": "Case Person",
            "role": "owner",
        },
    )
    # The same address in a different case is the same account, not a second one.
    resp = client.post(
        "/api/auth/login",
        json={"email": "mixedcase@example.com", "password": "correct-horse-battery"},
    )
    assert resp.status_code == 200


def test_signup_rejects_duplicate_email(client: TestClient, make_user) -> None:
    existing = make_user(role="owner", email="taken@example.com")
    resp = client.post(
        "/api/auth/signup",
        json={
            "email": existing["user"]["email"],
            "password": "a-different-password",
            "full_name": "Impostor",
            "role": "cleaner",
        },
    )
    assert resp.status_code == 409


def test_signup_refuses_to_create_an_admin(client: TestClient) -> None:
    """Admins approve IDs and background checks. That switch is not self-service."""
    resp = client.post(
        "/api/auth/signup",
        json={
            "email": "wannabe@example.com",
            "password": "correct-horse-battery",
            "full_name": "Wannabe Admin",
            "role": "admin",
        },
    )
    assert resp.status_code == 422


def test_signup_rejects_a_short_password(client: TestClient) -> None:
    resp = client.post(
        "/api/auth/signup",
        json={
            "email": "short@example.com",
            "password": "short",
            "full_name": "Short Password",
            "role": "owner",
        },
    )
    assert resp.status_code == 422


def test_login_succeeds_with_the_right_password(client: TestClient, make_user) -> None:
    user = make_user(role="cleaner")
    resp = client.post(
        "/api/auth/login",
        json={"email": user["user"]["email"], "password": user["password"]},
    )
    assert resp.status_code == 200
    assert resp.json()["user"]["id"] == user["user"]["id"]


def test_login_rejects_a_wrong_password(client: TestClient, make_user) -> None:
    user = make_user(role="owner")
    resp = client.post(
        "/api/auth/login",
        json={"email": user["user"]["email"], "password": "not-the-password"},
    )
    assert resp.status_code == 401


def test_login_for_an_unknown_email_looks_identical_to_a_wrong_password(
    client: TestClient, make_user
) -> None:
    """The response must not reveal whether the account exists."""
    user = make_user(role="owner")
    wrong_password = client.post(
        "/api/auth/login",
        json={"email": user["user"]["email"], "password": "not-the-password"},
    )
    unknown_email = client.post(
        "/api/auth/login",
        json={"email": "nobody@example.com", "password": "not-the-password"},
    )
    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()


def test_login_rejects_a_deactivated_account(client: TestClient, make_user, db) -> None:
    from app.models import User

    user = make_user(role="cleaner")
    db_user = db.get(User, __import__("uuid").UUID(user["user"]["id"]))
    db_user.is_active = False
    db.commit()

    resp = client.post(
        "/api/auth/login",
        json={"email": user["user"]["email"], "password": user["password"]},
    )
    assert resp.status_code == 403


def test_me_requires_a_token(client: TestClient) -> None:
    assert client.get("/api/auth/me").status_code == 401


def test_me_rejects_a_garbage_token(client: TestClient) -> None:
    resp = client.get("/api/auth/me", headers={"Authorization": "Bearer not.a.token"})
    assert resp.status_code == 401


def test_me_rejects_a_token_signed_with_the_wrong_key(client: TestClient, make_user) -> None:
    import jwt

    user = make_user(role="owner")
    forged = jwt.encode(
        {"sub": user["user"]["id"], "role": "admin", "type": "access", "exp": 9_999_999_999},
        "the-wrong-secret",
        algorithm="HS256",
    )
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {forged}"})
    assert resp.status_code == 401


def test_expired_token_is_rejected(client: TestClient, make_user) -> None:
    from datetime import timedelta

    from app.core.security import create_access_token

    user = make_user(role="owner")
    expired = create_access_token(
        subject=user["user"]["id"], role="owner", expires_delta=timedelta(minutes=-5)
    )
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {expired}"})
    assert resp.status_code == 401


class TestRoleGate:
    """The role gate is the whole authorization story at phase 1.

    Every later surface — the vetting queue, the bench board, accepting a bid —
    hangs off `require_role`, so it is worth proving it actually excludes.
    """

    def test_admin_route_admits_an_admin(self, client: TestClient, admin_user) -> None:
        resp = client.get("/api/auth/admin-check", headers=admin_user["auth"])
        assert resp.status_code == 200
        assert resp.json()["role"] == "admin"

    def test_admin_route_refuses_an_owner(self, client: TestClient, make_user) -> None:
        owner = make_user(role="owner")
        resp = client.get("/api/auth/admin-check", headers=owner["auth"])
        assert resp.status_code == 403

    def test_admin_route_refuses_a_cleaner(self, client: TestClient, make_user) -> None:
        cleaner = make_user(role="cleaner")
        resp = client.get("/api/auth/admin-check", headers=cleaner["auth"])
        assert resp.status_code == 403

    def test_admin_route_refuses_an_anonymous_caller(self, client: TestClient) -> None:
        assert client.get("/api/auth/admin-check").status_code == 401

    def test_a_forged_role_claim_does_not_grant_admin(
        self, client: TestClient, make_user
    ) -> None:
        """Authorization reads the database row, not the token's role claim.

        A token's claims are only as trustworthy as its signature; the role a
        request gets is the one on the user record.
        """
        from app.core.security import create_access_token

        owner = make_user(role="owner")
        token = create_access_token(subject=owner["user"]["id"], role="admin")
        resp = client.get("/api/auth/admin-check", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403
