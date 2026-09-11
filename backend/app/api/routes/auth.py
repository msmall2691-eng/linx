"""Signup, login, and identity endpoints for all three roles."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_role
from app.config import settings
from app.core.security import create_access_token, hash_password, verify_password
from app.db import get_db
from app.models.enums import UserRole
from app.models.user import User
from app.schemas.auth import LoginRequest, SignupRequest, TokenResponse, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])


def _token_response(user: User) -> TokenResponse:
    token = create_access_token(subject=user.id, role=user.role.value)
    return TokenResponse(
        access_token=token,
        expires_in=settings.access_token_expire_minutes * 60,
        user=UserOut.model_validate(user),
    )


@router.post("/signup", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def signup(payload: SignupRequest, db: Session = Depends(get_db)) -> TokenResponse:
    """Create an owner or cleaner account and return a token for it.

    Admin accounts are rejected by the request schema — they are created
    deliberately, not through this form.
    """
    user = User(
        email=payload.email,
        hashed_password=hash_password(payload.password),
        full_name=payload.full_name.strip(),
        phone=payload.phone,
        role=payload.role,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        # Unique violation on email. Racing signups land here too, which is
        # why this catches the constraint rather than pre-checking.
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with that email already exists",
        ) from None

    db.refresh(user)
    return _token_response(user)


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    user = db.execute(select(User).where(User.email == payload.email)).scalar_one_or_none()

    # Same error and roughly the same work whether the email is unknown or the
    # password is wrong, so the response cannot be used to enumerate accounts.
    if user is None or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is deactivated",
        )

    return _token_response(user)


@router.get("/me", response_model=UserOut)
def read_me(user: User = Depends(get_current_user)) -> User:
    return user


@router.get("/admin-check", response_model=UserOut, include_in_schema=False)
def admin_check(user: User = Depends(require_role(UserRole.ADMIN))) -> User:
    """Smallest possible admin-gated route.

    It exists so the role gate itself is exercised by the test suite before any
    real admin surface (the vetting queue, phase 3) is built on top of it.
    """
    return user
