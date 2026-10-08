"""Unit tests for one-time login codes (src/api/services/login_codes.py).

Covers the whole OTP contract: account state checks, hashed storage,
single use, expiry, attempt lockout, resend cooldown and the purge that
keeps SMTP failures retryable.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.api.db.database import LoginCodesRepository, UsersRepository
from src.api.services.email import EmailService
from src.api.services.login_codes import (
    LoginCodeError,
    _hash_code,
    request_code,
    verify_code,
)
from src.core.config import settings

EMAIL = "ana@example.com"


def _capture_codes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Replace the sender with a recorder so tests can read the plaintext code.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        A list that will collect ``(email, code)`` tuples as codes are sent.
    """
    sent: list[tuple[str, str]] = []

    async def fake_send(self: EmailService, email: str, code: str) -> None:
        sent.append((email, code))

    monkeypatch.setattr(EmailService, "send_login_code_email", fake_send)
    return sent


def _disable_smtp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the SMTP settings into the unconfigured state.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    for field in ("smtp_host", "smtp_user", "smtp_password", "mail_from"):
        monkeypatch.setattr(settings, field, "")


async def _seed_user(email: str = EMAIL, role: str = "viewer", status: str = "active") -> dict:
    """Insert a user row in the requested state.

    Args:
        email: Email of the account.
        role: Role to assign.
        status: Initial status ('pending' rows keep their state).

    Returns:
        The stored user row as a dict.
    """
    user = await UsersRepository().create(email, role=role)
    if status != "pending":
        await UsersRepository().set_status(int(user["id"]), status, role)
    return user


class TestRequestCode:
    """request_code(): validation, cooldown and delivery."""

    @pytest.mark.parametrize("email", ["", "   "])
    async def test_blank_email_is_rejected(self, email: str) -> None:
        """A blank email fails fast before touching the database."""
        with pytest.raises(LoginCodeError, match="Introduce tu email"):
            await request_code(email)

    async def test_unknown_account_is_rejected(self) -> None:
        """An email with no registration is told to register first."""
        with pytest.raises(LoginCodeError, match="Regístrate"):
            await request_code("ghost@example.com")

    async def test_pending_account_is_rejected(self) -> None:
        """A pending account cannot pull a code until the admin approves it."""
        await _seed_user(status="pending")
        with pytest.raises(LoginCodeError, match="pendiente"):
            await request_code(EMAIL)

    async def test_disabled_account_is_rejected(self) -> None:
        """A disabled account is told to contact the administrator."""
        await _seed_user(status="disabled")
        with pytest.raises(LoginCodeError, match="desactivada"):
            await request_code(EMAIL)

    async def test_unconfigured_smtp_fails_and_purges_the_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without SMTP the user gets a clear error and no stale code lingers."""
        _disable_smtp(monkeypatch)
        await _seed_user()

        with pytest.raises(LoginCodeError, match="correo no está configurado"):
            await request_code(EMAIL)

        assert await LoginCodesRepository().latest(EMAIL) is None

    async def test_send_failure_purges_the_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A transport error also purges so the retry does not hit the cooldown."""
        await _seed_user()
        monkeypatch.setattr(
            EmailService,
            "send_login_code_email",
            AsyncMock(side_effect=RuntimeError("boom")),
        )

        with pytest.raises(LoginCodeError, match="correo no está configurado"):
            await request_code(EMAIL)

        assert await LoginCodesRepository().latest(EMAIL) is None

    async def test_stores_only_a_hash_and_delivers_six_digits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The table never holds the plaintext: a 6-digit email code goes out."""
        sent = _capture_codes(monkeypatch)
        await _seed_user()

        await request_code(EMAIL)

        assert len(sent) == 1
        email, code = sent[0]
        assert email == EMAIL
        assert len(code) == 6 and code.isdigit()
        row = await LoginCodesRepository().latest(EMAIL)
        assert row is not None
        assert row["code_hash"] != code
        assert row["code_hash"] == _hash_code(EMAIL, code)

    async def test_resend_within_cooldown_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A second request right away asks the user to wait."""
        sent = _capture_codes(monkeypatch)
        await _seed_user()
        await request_code(EMAIL)

        with pytest.raises(LoginCodeError, match="Espera"):
            await request_code(EMAIL)

        # The original row survives untouched and no second mail went out.
        assert len(sent) == 1
        assert await LoginCodesRepository().latest(EMAIL) is not None

    async def test_resend_after_cooldown_is_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Once the cooldown lapses a fresh code replaces the previous one."""
        sent = _capture_codes(monkeypatch)
        await _seed_user()
        await request_code(EMAIL)
        monkeypatch.setattr(settings, "login_code_cooldown_seconds", 0)

        await request_code(EMAIL)

        assert len(sent) == 2


class TestVerifyCode:
    """verify_code(): matching, single use, attempts and expiry."""

    async def test_blank_fields_are_rejected(self) -> None:
        """Email and code are both mandatory."""
        with pytest.raises(LoginCodeError, match="obligatorios"):
            await verify_code("", "")
        with pytest.raises(LoginCodeError, match="obligatorios"):
            await verify_code(EMAIL, "")

    async def test_no_code_was_requested(self) -> None:
        """Without a stored code the user is told to request one."""
        with pytest.raises(LoginCodeError, match="Solicita uno nuevo"):
            await verify_code(EMAIL, "123456")

    async def test_happy_path_consumes_the_code_and_returns_the_user(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The right code logs in once and stamps last_login_at."""
        sent = _capture_codes(monkeypatch)
        await _seed_user(role="user")
        await request_code(EMAIL)

        user = await verify_code(EMAIL, sent[0][1])

        assert user["email"] == EMAIL
        assert user["status"] == "active"
        assert user["role"] == "user"
        stored = await UsersRepository().get_by_email(EMAIL)
        assert stored is not None and stored["last_login_at"] is not None
        row = await LoginCodesRepository().latest(EMAIL)
        assert row is not None and row["consumed_at"]

    async def test_code_is_single_use(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Replaying a consumed code fails even seconds later."""
        sent = _capture_codes(monkeypatch)
        await _seed_user()
        await request_code(EMAIL)
        await verify_code(EMAIL, sent[0][1])

        with pytest.raises(LoginCodeError, match="Solicita uno nuevo"):
            await verify_code(EMAIL, sent[0][1])

    async def test_wrong_code_records_an_attempt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Each miss is counted so brute force is bounded."""
        sent = _capture_codes(monkeypatch)
        await _seed_user()
        await request_code(EMAIL)
        wrong = "000000" if sent[0][1] != "000000" else "111111"

        with pytest.raises(LoginCodeError, match="incorrecto"):
            await verify_code(EMAIL, wrong)

        row = await LoginCodesRepository().latest(EMAIL)
        assert row is not None and int(row["attempts"]) == 1

    async def test_lockout_after_max_attempts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The fifth miss locks the code; later guesses learn nothing new."""
        sent = _capture_codes(monkeypatch)
        await _seed_user()
        await request_code(EMAIL)
        wrong = "000000" if sent[0][1] != "000000" else "111111"
        max_attempts = settings.login_code_max_attempts

        for attempt in range(1, max_attempts):
            with pytest.raises(LoginCodeError, match="incorrecto"):
                await verify_code(EMAIL, wrong)
        with pytest.raises(LoginCodeError, match="Demasiados intentos"):
            await verify_code(EMAIL, wrong)
        with pytest.raises(LoginCodeError, match="Demasiados intentos"):
            await verify_code(EMAIL, sent[0][1])

    async def test_expired_code_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A code older than the TTL cannot be redeemed."""
        sent = _capture_codes(monkeypatch)
        monkeypatch.setattr(settings, "login_code_ttl_seconds", -1)
        await _seed_user()
        await request_code(EMAIL)

        with pytest.raises(LoginCodeError, match="caducado"):
            await verify_code(EMAIL, sent[0][1])

    async def test_disabled_account_cannot_redeem_a_live_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Disabling between request and verify blocks the login."""
        sent = _capture_codes(monkeypatch)
        user = await _seed_user()
        await request_code(EMAIL)
        await UsersRepository().set_status(int(user["id"]), "disabled")

        with pytest.raises(LoginCodeError, match="no disponible"):
            await verify_code(EMAIL, sent[0][1])


class TestHashBinding:
    """_hash_code(): codes are meaningless without their email."""

    def test_same_code_different_email_hashes_differ(self) -> None:
        """A stolen hash cannot be replayed from another account."""
        assert _hash_code("a@example.com", "123456") != _hash_code("b@example.com", "123456")

    def test_hash_is_case_insensitive_on_email(self) -> None:
        """Case tricks in the email do not change the binding."""
        assert _hash_code("Ana@Example.com", "123456") == _hash_code("ana@example.com", "123456")

    def test_hash_is_not_the_plaintext(self) -> None:
        """The stored digest never contains the code itself."""
        digest = _hash_code("ana@example.com", "123456")
        assert "123456" not in digest
