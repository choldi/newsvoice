"""Unit tests for the Phase 7.10 single-URL article extraction service."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.api.models.article import ArticleIn
from src.api.services.url_article import (
    UrlArticleService,
    UrlExtractionError,
    extract_article,
    validate_url,
)

ARTICLE_HTML = """<!doctype html>
<html lang="es"><head><title>El Gobierno aprueba el presupuesto - El Pais</title>
<meta name="author" content="Ana Ruiz"></head>
<body><nav>Politica Economia Deportes</nav>
<article><h1>El Gobierno aprueba el presupuesto</h1>
<p>El Consejo de Ministros aprobó este jueves el presupuesto general del Estado con un
crecimiento del 3,5% para 2027, segun confirmó el ministro de Hacienda en rueda de prensa.</p>
<p>La oposicion criticó la medida y pidio una revision de los datos publicados por el INE
el mes pasado.</p></article>
<footer>(c) 2026 - Todos los derechos reservados</footer></body></html>"""

SHELL_HTML = """<!doctype html>
<html><head><title>App</title></head>
<body><nav>Entrar Registrarse Ayuda</nav><div id="root"></div>
<script src="/bundle.js"></script></body></html>"""


def _response(
    status: int = 200, body: bytes = b"", headers: dict[str, str] | None = None
) -> httpx.Response:
    """Build a standalone httpx.Response usable without a transport.

    Args:
        status: HTTP status code.
        body: Raw response body.
        headers: Optional response headers.

    Returns:
        A response object with request set (required for .text).
    """
    request = httpx.Request("GET", "https://example.com/article")
    return httpx.Response(status, content=body, headers=headers, request=request)


class _FakeClient:
    """Stand-in for httpx.AsyncClient returning one canned answer."""

    def __init__(self, response: httpx.Response | None = None, error: Exception | None = None):
        """Store the canned answer or the error to raise.

        Args:
            response: Response returned by get().
            error: Exception raised by get() instead.
        """
        self._response = response
        self._error = error

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def get(self, url: str) -> httpx.Response:  # noqa: ARG002
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response


def _patch_client(response: httpx.Response | None = None, error: Exception | None = None) -> object:
    """Patch httpx.AsyncClient inside the service with a canned client."""
    fake = _FakeClient(response=response, error=error)
    return patch(
        "src.api.services.url_article.httpx.AsyncClient",
        lambda **kwargs: fake,
    )


# ---------------------------------------------------------------------------
# validate_url
# ---------------------------------------------------------------------------


def test_validate_url_accepts_https() -> None:
    """A well-formed https URL passes unchanged apart from trimming."""
    assert validate_url("  https://example.com/a  ") == "https://example.com/a"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "ftp://example.com/a",
        "javascript:alert(1)",
        "/relative/path",
        "https://",
        "https://example.com/" + "a" * 3000,
    ],
)
def test_validate_url_rejects_unusable_values(bad: str) -> None:
    """Blank, relative, non-http and over-long URLs are rejected permanently."""
    with pytest.raises(UrlExtractionError):
        validate_url(bad)


# ---------------------------------------------------------------------------
# extract_article
# ---------------------------------------------------------------------------


def test_extract_article_returns_body_and_metadata() -> None:
    """The article keeps its body, title, source and publication date."""
    article = extract_article(ARTICLE_HTML, "https://elpais.com/economia/2026/aprobacion")

    assert isinstance(article, ArticleIn)
    assert article.title == "El Gobierno aprueba el presupuesto"
    assert "3,5%" in (article.content or "")
    assert "oposicion criticó" in (article.content or "")
    assert article.link == "https://elpais.com/economia/2026/aprobacion"
    assert article.source == "elpais.com"
    # Boilerplate (nav, footer, scripts) must not survive extraction.
    assert "Registrarse" not in (article.content or "")
    assert "Todos los derechos reservados" not in (article.content or "")


def test_extract_article_rejects_a_page_without_a_body() -> None:
    """A JavaScript shell has no readable article body."""
    with pytest.raises(UrlExtractionError, match="no readable article body"):
        extract_article(SHELL_HTML, "https://app.example.com/dashboard")


def test_extract_article_falls_back_to_the_title_tag() -> None:
    """When metadata has no title, the <title> tag is used."""
    html = ARTICLE_HTML.replace(
        "<title>El Gobierno aprueba el presupuesto - El Pais</title>",
        "<title>Titulo de respaldo</title>",
    )
    with patch("src.api.services.url_article.trafilatura.extract_metadata", return_value=None):
        article = extract_article(html, "https://example.com/a")
    assert article.title == "Titulo de respaldo"


def test_extract_article_truncates_a_very_long_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bodies over url_summary_max_chars are cut with an explicit marker."""
    monkeypatch.setattr("src.core.config.settings.url_summary_max_chars", 500)
    paragraphs = "".join(
        f"<p>Parrafo {i}: el consejo de ministros aprobo la medida con un crecimiento del "
        "3,5% para el proximo ejercicio economico nacional.</p>"
        for i in range(20)
    )
    html = f"<html><body><article>{paragraphs}</article></body></html>"
    article = extract_article(html, "https://example.com/long")

    assert len(article.content or "") < 600
    assert "texto truncado a 500 caracteres" in (article.content or "")


# ---------------------------------------------------------------------------
# UrlArticleService
# ---------------------------------------------------------------------------


async def test_fetch_html_returns_the_page() -> None:
    """A 200 answer yields the decoded HTML."""
    with _patch_client(
        _response(200, ARTICLE_HTML.encode(), {"content-type": "text/html; charset=utf-8"})
    ):
        html = await UrlArticleService().fetch_html("https://example.com/a")
    assert "Gobierno" in html


async def test_fetch_html_treats_a_4xx_as_permanent() -> None:
    """4xx answers raise the permanent UrlExtractionError."""
    with _patch_client(_response(404, b"not found")):
        with pytest.raises(UrlExtractionError, match="HTTP 404"):
            await UrlArticleService().fetch_html("https://example.com/a")


async def test_fetch_html_allows_5xx_to_retry() -> None:
    """5xx answers surface as httpx errors so Temporal retries them."""
    with _patch_client(_response(503, b"unavailable")):
        with pytest.raises(httpx.HTTPStatusError):
            await UrlArticleService().fetch_html("https://example.com/a")


async def test_fetch_html_rejects_an_oversized_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bodies over url_fetch_max_bytes are refused without being parsed."""
    monkeypatch.setattr("src.core.config.settings.url_fetch_max_bytes", 10_000)
    with _patch_client(_response(200, b"x" * 10_001)):
        with pytest.raises(UrlExtractionError, match="larger than"):
            await UrlArticleService().fetch_html("https://example.com/a")


async def test_fetch_html_rejects_an_empty_body() -> None:
    """An empty answer cannot be an article."""
    with _patch_client(_response(200, b"")):
        with pytest.raises(UrlExtractionError, match="empty body"):
            await UrlArticleService().fetch_html("https://example.com/a")


async def test_fetch_html_propagates_transport_errors() -> None:
    """Connection failures propagate for Temporal to retry."""
    with _patch_client(error=httpx.ConnectError("boom")):
        with pytest.raises(httpx.ConnectError):
            await UrlArticleService().fetch_html("https://example.com/a")


async def test_extract_from_url_wires_fetch_and_extraction() -> None:
    """The full path returns an ArticleIn ready for the digest."""
    with patch.object(
        UrlArticleService,
        "fetch_html",
        new=AsyncMock(return_value=ARTICLE_HTML),
    ):
        article = await UrlArticleService().extract_from_url("https://elpais.com/economia/1")

    assert isinstance(article, ArticleIn)
    assert article.title == "El Gobierno aprueba el presupuesto"
    assert "3,5%" in (article.content or "")
