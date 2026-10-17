"""One-time login codes (OTP) for non-admin users (Phase 6).

Users self-registered by email never store a password: every login emails a
fresh 6-digit code. Only its sha256 hash (bound to the email) reaches the
``login_codes`` table, and a code is single-use, expires after
``LOGIN_CODE_TTL_SECONDS``, allows at most ``LOGIN_CODE_MAX_ATTEMPTS``
failed tries and is rate-limited by ``LOGIN_CODE_COOLDOWN_SECONDS``.

Every failure raises :class:`LoginCodeError` whose message is written in
user-facing Spanish and is safe to flash straight into the GUI.
"""

from __future__ import annotations

import hashlib
import secrets
import smtplib
from datetime import UTC, datetime

import structlog

from src.api.db.database import LoginCodesRepository, UsersRepository
from src.api.services.email import EmailService
from src.core.config import settings

logger = structlog.get_logger()


class LoginCodeError(Exception):
    """User-facing login code failure; str(exc) is safe to show in the GUI."""


def _hash_code(email: str, code: str) -> str:
    """Hash a code bound to its email so a bare hash cannot be replayed.

    Args:
        email: Email the code was issued for.
        code: The 6-digit plaintext code.

    Returns:
        Hex sha256 digest of ``email:code``.
    """
    normalized = email.strip().lower()
    return hashlib.sha256(f"{normalized}:{code}".encode()).hexdigest()


def _parse_ts(value: str) -> datetime:
    """Parse a stored timestamp into an aware UTC datetime.

    Args:
        value: Raw timestamp string from SQLite.

    Returns:
        An aware UTC datetime (naive values are treated as UTC).
    """
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


async def request_code(email: str) -> None:
    """Validate the account and email it a fresh one-time login code.

    Args:
        email: Email asking for a code.

    Raises:
        LoginCodeError: Unknown, pending or disabled account; resend
            cooldown still running; or the email could not be sent (SMTP
            unconfigured or failing) — in which case the stored code is
            purged so the user can retry immediately.
    """
    normalized = email.strip()
    if not normalized:
        raise LoginCodeError("Introduce tu email.")

    user = await UsersRepository().get_by_email(normalized)
    if user is None:
        raise LoginCodeError("No existe ninguna cuenta con ese email. Regístrate primero.")
    if user["status"] == "pending":
        raise LoginCodeError("Tu cuenta está pendiente de validación por el administrador.")
    if user["status"] == "disabled":
        raise LoginCodeError("Cuenta desactivada. Contacta con el administrador.")

    codes = LoginCodesRepository()
    latest = await codes.latest(normalized)
    if latest is not None and not latest.get("consumed_at"):
        elapsed = (datetime.now(UTC) - _parse_ts(latest["last_sent_at"])).total_seconds()
        cooldown = settings.login_code_cooldown_seconds
        if elapsed < cooldown:
            wait = int(cooldown - elapsed) + 1
            raise LoginCodeError(f"Espera {wait} segundos antes de pedir otro código.")

    code = f"{secrets.randbelow(1_000_000):06d}"
    await codes.create(
        normalized,
        _hash_code(normalized, code),
        ttl_seconds=settings.login_code_ttl_seconds,
    )
    try:
        await EmailService().send_login_code_email(normalized, code)
    except (RuntimeError, smtplib.SMTPException) as exc:
        await codes.purge_for_email(normalized)
        logger.warning("login_code_send_failed", email=normalized, error=str(exc))
        raise LoginCodeError(
            "No se pudo enviar el código: el correo no está configurado (revisa SMTP_* en .env)."
        ) from exc
    logger.info("login_code_sent", email=normalized)


async def verify_code(email: str, code: str) -> dict:
    """Check a code and, on success, consume it and return the user row.

    Args:
        email: Email the code was issued for.
        code: The 6-digit code typed by the user.

    Returns:
        The user row (``status`` is guaranteed ``active``).

    Raises:
        LoginCodeError: Missing, unknown, expired, exhausted or wrong code,
            or an account that is no longer active.
    """
    normalized = email.strip()
    provided = code.strip()
    if not normalized or not provided:
        raise LoginCodeError("Email y código son obligatorios.")

    codes = LoginCodesRepository()
    row = await codes.latest(normalized)
    if row is None or row.get("consumed_at"):
        raise LoginCodeError("Código no válido. Solicita uno nuevo.")
    if await codes.is_expired(row):
        raise LoginCodeError("El código ha caducado. Solicita uno nuevo.")
    if int(row["attempts"]) >= settings.login_code_max_attempts:
        raise LoginCodeError("Demasiados intentos. Solicita un código nuevo.")

    if not secrets.compare_digest(str(row["code_hash"]), _hash_code(normalized, provided)):
        attempts = await codes.record_attempt(int(row["id"]))
        if attempts >= settings.login_code_max_attempts:
            raise LoginCodeError("Demasiados intentos. Solicita un código nuevo.")
        raise LoginCodeError("Código incorrecto.")

    if not await codes.consume(int(row["id"])):
        raise LoginCodeError("Código no válido. Solicita uno nuevo.")

    user = await UsersRepository().get_by_email(normalized)
    if user is None or user["status"] != "active":
        raise LoginCodeError("Cuenta no disponible. Contacta con el administrador.")
    await UsersRepository().touch_login(int(user["id"]))
    logger.info("login_code_verified", email=normalized, user_id=int(user["id"]))
    return user
