"""Unit tests for the EmailService (markdown->HTML rendering + SMTP send)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.api.services.email import EmailService, render_html

email_config = SimpleNamespace(
    smtp_host="smtp.gmail.com",
    smtp_port=587,
    smtp_user="me@gmail.com",
    smtp_password="app-password",
    mail_from="me@gmail.com",
    mail_from_name="News Engine",
)


def test_render_html_converts_markdown() -> None:
    """render_html turns markdown into an HTML document and strips emotion tags."""
    markdown_text = "[happy] # Titulo principal\n\n- Uno\n- Dos\n\n**negrita**"
    html = render_html(markdown_text, collection="tech", subject="subject")

    assert "<h1>Titulo principal</h1>" in html
    assert "<li>Uno</li>" in html
    assert "<strong>negrita</strong>" in html
    assert "[happy]" not in html
    assert "<html" in html


def test_render_html_includes_header_and_subject() -> None:
    """render_html includes the collection name and subject in the document."""
    html = render_html("# x", collection="health", subject="Daily News Digest heal")

    assert "health" in html
    assert "Daily News Digest heal" in html


def test_build_message_sets_headers_and_parts() -> None:
    """build_message creates a multipart message with correct headers."""
    service = EmailService(smtp_settings=email_config)
    message = service.build_message(
        ["one@example.com", "two@example.com"],
        "# Titulo\n\nContenido",
        collection="tech",
    )

    assert message["Subject"].startswith("Daily News Digest")
    assert "tech" in message["Subject"]
    assert message["From"] == "News Engine <me@gmail.com>"
    assert message["To"] == "one@example.com, two@example.com"

    parts = list(message.walk())
    types = {part.get_content_type() for part in parts}
    assert {"text/plain", "text/html"}.issubset(types)
    html_part = next(part for part in parts if part.get_content_type() == "text/html")
    assert "<h1>Titulo</h1>" in html_part.get_content()


def test_build_message_deduplicates_recipients() -> None:
    """build_message dedupes recipients case-insensitively and drops empties."""
    service = EmailService(smtp_settings=email_config)
    message = service.build_message(
        ["One@Example.com", "one@example.com", ""],
        "# Titulo",
        collection="tech",
    )

    assert message["To"] == "one@example.com"


def test_build_message_raises_without_valid_recipients() -> None:
    """build_message rejects an empty recipient list."""
    service = EmailService(smtp_settings=email_config)

    try:
        service.build_message([], "# Titulo", collection="tech")
    except ValueError as exc:
        assert "no valid email recipients" in str(exc).lower()
    else:
        raise AssertionError("expected ValueError")


def test_send_message_logs_in_and_sends_via_starttls() -> None:
    """Port 587 uses SMTP + STARTTLS and logs in before sending."""

    class FakeSMTP:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.ehlo_calls = 0
            self.login_called = False
            self.sent: object = None

        def __enter__(self) -> "FakeSMTP":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def ehlo(self) -> None:
            self.ehlo_calls += 1

        def starttls(self, context: object = None) -> None:
            return None

        def login(self, user: str, password: str) -> None:
            self.login_called = True

        def send_message(self, message: object) -> None:
            self.sent = message

    fake = FakeSMTP()
    service = EmailService(smtp_settings=email_config)
    message = service.build_message(["a@b.com"], "# Titulo", collection="tech")

    with patch("src.api.services.email.smtplib.SMTP", return_value=fake):
        service.send_message(message)

    assert fake.ehlo_calls >= 1
    assert fake.login_called is True
    assert fake.sent is not None


def test_send_message_raises_when_not_configured() -> None:
    """send_message raises a clear error when SMTP credentials are missing."""
    empty_config = SimpleNamespace(
        smtp_host="smtp.gmail.com",
        smtp_port=587,
        smtp_user="",
        smtp_password="",
        mail_from="",
        mail_from_name="",
    )
    service = EmailService(smtp_settings=email_config)
    message = service.build_message(["a@b.com"], "# x", collection="tech")
    unconfigured = EmailService(smtp_settings=empty_config)

    try:
        unconfigured.send_message(message)
    except RuntimeError as exc:
        assert "SMTP_HOST" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


async def test_send_digest_email_returns_status() -> None:
    """send_digest_email returns a status dict after sending."""
    service = EmailService(smtp_settings=email_config)
    with patch.object(service, "send_message", return_value=None) as mock_send:
        result = await service.send_digest_email(
            ["a@example.com", "b@example.com"],
            "# Titulo",
            collection="tech",
        )

    mock_send.assert_called_once()
    assert result["status"] == "ok"
    assert result["recipients"] == 2


async def test_deliver_email_activity_calls_service() -> None:
    """deliver_email_activity delegates to EmailService and returns its result."""
    from temporalio.testing import ActivityEnvironment

    from src.workers.activities import deliver_email_activity

    with patch(
        "src.workers.activities.EmailService.send_digest_email",
        new_callable=AsyncMock,
        return_value={"status": "ok", "recipients": 2},
    ) as mock_email:
        result = await ActivityEnvironment().run(
            deliver_email_activity,
            ["a@example.com", "b@example.com"],
            "# digest",
            "tech",
        )

    assert result["status"] == "ok"
    assert mock_email.await_count == 1
