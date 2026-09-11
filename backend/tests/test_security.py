"""Password hashing and token handling."""

from __future__ import annotations

import uuid
from datetime import timedelta

import jwt
import pytest

from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


def test_hash_does_not_contain_the_password() -> None:
    hashed = hash_password("correct-horse-battery")
    assert "correct-horse-battery" not in hashed
    assert hashed.startswith("$2b$")


def test_the_same_password_hashes_differently_each_time() -> None:
    """Distinct salts, so identical passwords are not identifiable as identical."""
    assert hash_password("same-password-here") != hash_password("same-password-here")


def test_verify_accepts_the_right_password_and_rejects_others() -> None:
    hashed = hash_password("correct-horse-battery")
    assert verify_password("correct-horse-battery", hashed)
    assert not verify_password("correct-horse-batteru", hashed)


def test_long_passwords_are_not_truncated_to_72_bytes() -> None:
    """bcrypt ignores everything past 72 bytes; the SHA-256 pre-hash prevents that.

    Without it, two passphrases sharing a 72-byte prefix would be
    interchangeable at login — a real hole for anyone using a long passphrase.
    """
    prefix = "x" * 72
    hashed = hash_password(prefix + "first-tail")
    assert verify_password(prefix + "first-tail", hashed)
    assert not verify_password(prefix + "second-tail", hashed)


def test_verify_returns_false_for_a_malformed_hash() -> None:
    """A corrupt row is a failed login, never a 500."""
    assert not verify_password("anything", "not-a-bcrypt-hash")


def test_token_round_trips() -> None:
    user_id = uuid.uuid4()
    payload = decode_access_token(create_access_token(subject=user_id, role="cleaner"))
    assert payload["sub"] == str(user_id)
    assert payload["role"] == "cleaner"


def test_expired_token_raises() -> None:
    token = create_access_token(
        subject=uuid.uuid4(), role="owner", expires_delta=timedelta(seconds=-1)
    )
    with pytest.raises(jwt.ExpiredSignatureError):
        decode_access_token(token)


def test_token_signed_with_another_key_raises() -> None:
    forged = jwt.encode(
        {"sub": str(uuid.uuid4()), "type": "access", "exp": 9_999_999_999},
        "some-other-secret",
        algorithm="HS256",
    )
    with pytest.raises(jwt.InvalidSignatureError):
        decode_access_token(forged)


def test_a_token_of_another_type_is_not_an_access_token() -> None:
    """Guards the door before refresh or password-reset tokens exist."""
    from app.config import settings

    other = jwt.encode(
        {"sub": str(uuid.uuid4()), "type": "password_reset", "exp": 9_999_999_999},
        settings.secret_key,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token(other)


def test_unsigned_token_is_rejected() -> None:
    """The classic `alg: none` forgery."""
    forged = jwt.encode(
        {"sub": str(uuid.uuid4()), "type": "access", "exp": 9_999_999_999},
        key="",
        algorithm="none",
    )
    with pytest.raises(jwt.PyJWTError):
        decode_access_token(forged)
