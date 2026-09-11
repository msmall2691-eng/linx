"""Request and response shapes for signup, login, and the current user."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.enums import UserRole


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=256)
    full_name: str = Field(min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=32)
    role: UserRole

    @field_validator("role")
    @classmethod
    def no_self_service_admin(cls, v: UserRole) -> UserRole:
        """Admins are made deliberately, never by signing up.

        Admins approve IDs and background checks — the one thing standing
        between "vetted" and "anyone can walk into a stranger's house". Self-
        service admin signup would hand that switch to whoever finds the form.
        """
        if v is UserRole.ADMIN:
            raise ValueError("admin accounts cannot be created through signup")
        return v

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        return v.strip().lower()


class LoginRequest(BaseModel):
    email: EmailStr
    password: str

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        return v.strip().lower()


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    full_name: str
    phone: str | None
    role: UserRole
    is_active: bool
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut
