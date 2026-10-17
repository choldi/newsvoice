"""Email delivery service for the daily news digest.

Converts the digest markdown into a styled HTML email and sends it via SMTP
(Gmail by default, configurable per-instance via .env).

The service is used exclusively by `deliver_email_activity` in the Temporal
workflow and follows the same async wrapper pattern as other services in the
codebase.
"""

import asyncio
import mimetypes
import smtplib
import ssl
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

import markdown as md_lib
import structlog
from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.core.config import Settings, get_settings
from src.core.prompts import strip_emotion_tags
from src.core.tracing import trace_function

logger = structlog.get_logger()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_JINJA_ENV = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
)

_DEFAULT_SSL_PORT = 465
_SMTP_TIMEOUT = 30


def _normalize_recipients(recipients: list[str]) -> list[str]:
    """Dedupe and strip whitespace from recipient addresses preserving order.

    Args:
        recipients: Raw email addresses.

    Returns:
        De-duplicated, non-empty addresses.
    """
    seen: set[str] = set()
    result: list[str] = []
    for addr in recipients:
        clean = addr.strip().lower()
        if clean and clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result


def _audio_mime_subtype(audio_path: str) -> str:
    """Return the RFC 2046 audio subtype for a file path.

    Args:
        audio_path: Path to the audio file.

    Returns:
        MIME audio subtype (wav, ogg, else mpeg).
    """
    suffix = Path(audio_path).suffix.lower()
    if suffix == ".ogg":
        return "ogg"
    if suffix == ".wav":
        return "wav"
    if suffix == ".m4a":
        return "mp4"
    return "mpeg"


def _subject(collection: str) -> str:
    """Build the email subject line.

    Args:
        collection: Collection name (e.g. 'tech').

    Returns:
        Subject string with the date of the current UTC day.
    """
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    return f"Daily News Digest \u00b7 {collection} \u00b7 {today}"


def render_html(markdown_text: str, collection: str, subject: str) -> str:
    """Convert digest markdown to an HTML email body.

    Strips OmniVoice emotion tags before conversion and renders the resulting
    HTML inside a minimal responsive email template.

    Args:
        markdown_text: The raw digest markdown.
        collection: Collection name for the header.
        subject: Full subject line for the <title> tag.

    Returns:
        Complete HTML document string ready to be embedded as a MIME part.
    """
    plain = strip_emotion_tags(markdown_text)
    body_html = md_lib.markdown(plain, extensions=["extra"])
    return _JINJA_ENV.get_template("email_digest.html").render(
        subject=subject,
        collection=collection,
        date=datetime.now(UTC).strftime("%Y-%m-%d"),
        body=body_html,
    )


class EmailService:
    """Render and send the digest as an HTML+plain email via SMTP.

    Args:
        smtp_settings: Optional settings override. Defaults to the global
            ``get_settings()`` instance.
    """

    def __init__(self, smtp_settings: Settings | None = None) -> None:
        """Initialize with an optional settings override.

        Args:
            smtp_settings: Settings object (defaults to global settings).
        """
        self._cfg = smtp_settings or get_settings()

    @property
    def _configured(self) -> bool:
        """Return whether all required SMTP credentials are set.

        Returns:
            True when smtp_host, smtp_user, smtp_password, and mail_from are
            all non-empty.
        """
        s = self._cfg
        return bool(s.smtp_host and s.smtp_user and s.smtp_password and s.mail_from)

    def build_message(
        self,
        recipients: list[str],
        markdown_text: str,
        collection: str,
        subject: str | None = None,
        audio_path: str | None = None,
        attachments: list[str] | None = None,
    ) -> EmailMessage:
        """Build a MIME email with an HTML and plain-text alternative.

        Args:
            recipients: List of recipient email addresses.
            markdown_text: Raw digest markdown.
            collection: Collection name.
            subject: Optional subject override; auto-generated when None.
            audio_path: Optional audio file to attach (missing file is ignored).
            attachments: Extra file paths to attach with a guessed MIME type
                (missing files are ignored); used by Phase 7.12 to ship the
                lesson `.md`.

        Returns:
            A ready-to-send ``EmailMessage`` instance.

        Raises:
            ValueError: If no valid recipients are provided.
        """
        valid_recipients = _normalize_recipients(recipients)
        if not valid_recipients:
            raise ValueError("No valid email recipients provided")
        msg_subject = subject or _subject(collection)
        html_body = render_html(markdown_text, collection=collection, subject=msg_subject)
        plain_body = strip_emotion_tags(markdown_text)

        msg = EmailMessage()
        msg["Subject"] = msg_subject
        name = self._cfg.mail_from_name
        msg["From"] = f"{name} <{self._cfg.mail_from}>" if name else self._cfg.mail_from
        msg["To"] = ", ".join(valid_recipients)
        msg.set_content(plain_body)
        msg.add_alternative(html_body, subtype="html")

        if audio_path:
            audio_file = Path(audio_path)
            if audio_file.exists():
                audio_payload = audio_file.read_bytes()
                msg.add_attachment(
                    audio_payload,
                    maintype="audio",
                    subtype=_audio_mime_subtype(audio_path),
                    filename=audio_file.name,
                )
            else:
                logger.warning("email_audio_missing", audio_path=audio_path)

        for extra_path in attachments or []:
            extra_file = Path(extra_path)
            if not extra_file.exists():
                logger.warning("email_attachment_missing", attachment=extra_path)
                continue
            guessed, _ = mimetypes.guess_type(str(extra_file))
            if guessed:
                maintype, _, subtype = guessed.partition("/")
            else:
                maintype, subtype = "application", "octet-stream"
            msg.add_attachment(
                extra_file.read_bytes(),
                maintype=maintype,
                subtype=subtype,
                filename=extra_file.name,
            )

        return msg

    def send_message(self, message: EmailMessage) -> None:
        """Send the message synchronously via SMTP (blocking).

        Uses implicit TLS (SMTP_SSL) on port 465 and STARTTLS on all other
        ports. Logs in when ``smtp_user`` is configured.

        Args:
            message: The ready-to-send EmailMessage.

        Raises:
            RuntimeError: When SMTP credentials are missing.
            smtplib.SMTPException: On connection or authentication failure.
        """
        if not self._configured:
            raise RuntimeError(
                "SMTP is not configured; set SMTP_HOST, SMTP_USER, SMTP_PASSWORD,"
                " MAIL_FROM in your .env"
            )
        s = self._cfg
        context = ssl.create_default_context()

        if s.smtp_port == _DEFAULT_SSL_PORT:
            with smtplib.SMTP_SSL(
                s.smtp_host, s.smtp_port, context=context, timeout=_SMTP_TIMEOUT
            ) as server:
                if s.smtp_user and s.smtp_password:
                    server.login(s.smtp_user, s.smtp_password)
                server.send_message(message)
        else:
            with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=_SMTP_TIMEOUT) as server:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
                if s.smtp_user and s.smtp_password:
                    server.login(s.smtp_user, s.smtp_password)
                server.send_message(message)

    def build_login_code_message(self, email: str, code: str) -> EmailMessage:
        """Build the plain-text email carrying a one-time login code (Phase 6).

        Args:
            email: Recipient address (the account asking for the code).
            code: The 6-digit plaintext code; never persisted, only emailed.

        Returns:
            A ready-to-send ``EmailMessage`` with a plain-text body.
        """
        msg = EmailMessage()
        msg["Subject"] = "News Engine · tu código de acceso"
        name = self._cfg.mail_from_name
        msg["From"] = f"{name} <{self._cfg.mail_from}>" if name else self._cfg.mail_from
        msg["To"] = email
        msg.set_content(
            f"Tu código de acceso es: {code}\n\n"
            "Caduca en unos minutos y solo puede usarse una vez.\n"
            "Si no has solicitado este código, ignora este mensaje.\n"
        )
        return msg

    def send_login_code(self, email: str, code: str) -> None:
        """Send the 6-digit one-time login code synchronously (blocking).

        Args:
            email: Recipient address.
            code: The plaintext code to deliver.

        Raises:
            RuntimeError: When SMTP credentials are missing.
            smtplib.SMTPException: On connection or authentication failure.
        """
        self.send_message(self.build_login_code_message(email, code))

    async def send_login_code_email(self, email: str, code: str) -> None:
        """Send the one-time login code asynchronously.

        Wraps the blocking SMTP call in ``asyncio.to_thread`` so the
        coroutine is safe inside FastAPI handlers.

        Args:
            email: Recipient address.
            code: The plaintext code to deliver.

        Raises:
            RuntimeError: When SMTP credentials are missing.
            smtplib.SMTPException: On connection or authentication failure.
        """
        await asyncio.to_thread(self.send_login_code, email, code)

    @trace_function(component="api.services.email")
    async def send_digest_email(
        self,
        recipients: list[str],
        markdown_text: str,
        collection: str,
        subject: str | None = None,
        audio_path: str | None = None,
        attachments: list[str] | None = None,
    ) -> dict[str, str]:
        """Build and send the digest email asynchronously.

        Wraps the blocking SMTP calls in ``asyncio.to_thread`` so the
        coroutine can safely be used inside a Temporal activity.

        Args:
            recipients: Destination email addresses.
            markdown_text: Digest markdown content.
            collection: Collection name.
            subject: Optional subject line override.
            audio_path: Optional audio file to attach to the email.
            attachments: Extra files to attach (Phase 7.12 lesson text).

        Returns:
            Dict with status and recipient count.

        Raises:
            ValueError: If no valid recipients are provided.
            RuntimeError: If SMTP is not configured.
            smtplib.SMTPException: On SMTP errors.
        """
        valid_recipients = _normalize_recipients(recipients)
        message = self.build_message(
            valid_recipients,
            markdown_text,
            collection,
            subject=subject,
            audio_path=audio_path,
            attachments=attachments,
        )
        await asyncio.to_thread(self.send_message, message)
        logger.info(
            "email_sent",
            collection=collection,
            recipients=len(valid_recipients),
        )
        return {"status": "ok", "recipients": len(valid_recipients)}
