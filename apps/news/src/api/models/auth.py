"""Pydantic models for multi-user access (Phase 6).

The admin is a local credential in ``.env`` and is not a row; these models
cover the self-registered users, their one-time login codes and the session
tokens that replace the legacy raw-password cookie.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.api.authz import Role


class RegisterIn(BaseModel):
    """Input payload of the self-registration form.

    Only the email is collected: the account starts as ``pending`` until the
    admin approves it and assigns a role.
    """

    email: str = Field(..., max_length=254, description="Email of the applicant")


class UserDB(BaseModel):
    """Persisted non-admin user (migration 012 ``users`` table)."""

    id: int
    email: str
    role: Role
    status: str = Field(description="'pending' | 'active' | 'disabled'")
    created_at: str | None = None
    approved_at: str | None = None
    last_login_at: str | None = None


class SessionDB(BaseModel):
    """Persisted session token (migration 012 ``sessions`` table)."""

    token: str
    user_id: int | None = None
    identity: str = Field(description="Display identity: email or admin username")
    role: Role
    expires_at: str


class LoginCodeDB(BaseModel):
    """Persisted one-time login code (migration 012 ``login_codes`` table).

    Only ``code_hash`` (sha256) is stored — never the plaintext code.
    """

    id: int
    email: str
    code_hash: str
    attempts: int
    expires_at: str
    consumed_at: str | None = None
    last_sent_at: str | None = None
