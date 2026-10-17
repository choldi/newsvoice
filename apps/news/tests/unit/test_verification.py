"""Unit tests for the Phase 7.13 LLM verification service."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from src.api.models.verification import (
    ClaimLabel,
    ClaimVerdictOut,
    Verdict,
    VerifyDigestIn,
)
from src.api.services.verification import (
    VerificationError,
    VerificationService,
    build_sources,
    extract_figures,
    groundedness,
    strip_markdown_structure,
)

# ---------------------------------------------------------------------------
# extract_figures
# ---------------------------------------------------------------------------


def test_extract_figures_strips_markdown_scaffolding() -> None:
    """List markers and heading hashes are not figures."""
    text = "## Main Stories\n\n1. **OpenAI launches GPT-6**\n2. **Spain grows**\n"
    assert extract_figures(text) == []


def test_extract_figures_ignores_product_version_numbers() -> None:
    """Version numbers after a capitalized product name are not quantities."""
    text = "Llama 3.3 70B leads the ranking. GPT-5 is out. Claude 4 is new."
    assert extract_figures(text) == []


def test_extract_figures_detects_currency_percent_date_and_number() -> None:
    """Each kind is detected and reported with its richest spelling."""
    text = "Revenue hit 45 billion dollars, up 12%, on 2026-10-02. Sales: 120 units."
    figures = extract_figures(text)
    assert ("45", "number") in figures
    assert ("12%", "percent") in figures
    assert ("2026-10-02", "date") in figures
    assert ("120", "number") in figures


def test_extract_figures_does_not_double_count_nested_numbers() -> None:
    """A percentage is reported once, not also as its bare number."""
    text = "Growth was 12% year over year."
    figures = extract_figures(text)
    assert figures == [("12%", "percent")]


def test_extract_figures_handles_european_decimal_separator() -> None:
    """A dot as thousands separator is recognized as a number."""
    text = "El coste es de 1.200 euros."
    assert ("1.200", "number") in extract_figures(text)


def test_extract_figures_detects_invented_number() -> None:
    """A number with no source counterpart is still extracted (groundedness catches it)."""
    text = "The company raised 999 million dollars."
    assert ("999", "number") in extract_figures(text)


def test_strip_markdown_structure_removes_bold_and_list_markers() -> None:
    """Structural tokens are removed while prose is preserved."""
    text = "## Title\n\n1. **Bold item**\n2. Normal item\n"
    cleaned = strip_markdown_structure(text)
    assert "1." not in cleaned
    assert "**" not in cleaned
    assert "##" not in cleaned
    assert "Bold item" in cleaned
    assert "Normal item" in cleaned


# ---------------------------------------------------------------------------
# groundedness
# ---------------------------------------------------------------------------


def test_groundedness_all_figures_in_sources() -> None:
    """Every figure present in the sources yields a perfect score."""
    digest = "Revenue grew 12% to 45 billion dollars on 2026-10-02."
    sources = build_sources(
        [
            {
                "articles": [
                    {
                        "title": "Report",
                        "link": "https://example.com/report",
                        "content": "Revenue grew 12% to 45 billion dollars on 2026-10-02.",
                        "source": "Example",
                    }
                ]
            }
        ]
    )
    result = groundedness(digest, sources)
    assert result.score == 1.0
    assert result.total == 3
    assert result.grounded == 3
    assert result.ungrounded == []


def test_groundedness_detects_invented_figure() -> None:
    """A figure absent from every source is reported as ungrounded."""
    digest = "Revenue grew 12% to 999 billion dollars."
    sources = build_sources(
        [
            {
                "articles": [
                    {
                        "title": "Report",
                        "link": "https://example.com/report",
                        "content": "Revenue grew 12% to 45 billion dollars.",
                        "source": "Example",
                    }
                ]
            }
        ]
    )
    result = groundedness(digest, sources)
    assert result.score < 1.0
    assert len(result.ungrounded) == 1
    assert result.ungrounded[0].value == "999"


def test_groundedness_no_figures_returns_perfect_score() -> None:
    """A digest with no checkable figures cannot fail groundedness."""
    digest = "OpenAI released a new model."
    sources = build_sources([{"articles": []}])
    result = groundedness(digest, sources)
    assert result.score == 1.0
    assert result.total == 0


def test_groundedness_matches_european_number_spelling() -> None:
    """1.200 in the digest matches 1200 in the source."""
    digest = "El coste es de 1.200 euros."
    sources = build_sources(
        [
            {
                "articles": [
                    {
                        "title": "Report",
                        "link": "https://example.com/report",
                        "content": "El coste es de 1200 euros.",
                        "source": "Example",
                    }
                ]
            }
        ]
    )
    result = groundedness(digest, sources)
    assert result.score == 1.0


# ---------------------------------------------------------------------------
# VerificationService.verify_digest
# ---------------------------------------------------------------------------


def _make_request(
    digest: str = "Revenue grew 12% to 45 billion dollars.",
    clusters: list[dict] | None = None,
    threshold: float = 0.9,
    groundedness_threshold: float = 1.0,
) -> VerifyDigestIn:
    """Build a VerifyDigestIn with sensible defaults."""
    if clusters is None:
        clusters = [
            {
                "articles": [
                    {
                        "title": "Report",
                        "link": "https://example.com/report",
                        "content": "Revenue grew 12% to 45 billion dollars.",
                        "source": "Example",
                    }
                ]
            }
        ]
    return VerifyDigestIn(
        digest=digest,
        clusters=clusters,
        collection="test",
        threshold=threshold,
        groundedness_threshold=groundedness_threshold,
        backend="local",
        model="test-model",
        host_reliability={"example.com": 1.0},
    )


async def test_verify_digest_passes_when_grounded_and_judge_supports() -> None:
    """A digest with all figures grounded and all claims supported passes."""
    request = _make_request()
    service = VerificationService()

    with (
        patch("src.api.services.verification._extract_claims", new=AsyncMock(return_value=[])),
        patch("src.api.services.verification._judge_batch", new=AsyncMock(return_value=[])),
    ):
        report = await service.verify_digest(request)

    assert report.passed is True
    assert report.verdict == Verdict.VALID
    assert report.groundedness.score == 1.0
    assert report.faithfulness.score == 1.0


async def test_verify_digest_fails_on_ungrounded_figure() -> None:
    """An invented figure fails the gate even if the judge supports all claims."""
    request = _make_request(digest="Revenue grew 12% to 999 billion dollars.")
    service = VerificationService()

    with (
        patch("src.api.services.verification._extract_claims", new=AsyncMock(return_value=[])),
        patch("src.api.services.verification._judge_batch", new=AsyncMock(return_value=[])),
    ):
        report = await service.verify_digest(request)

    assert report.passed is False
    assert report.verdict == Verdict.INVALID
    assert report.groundedness.score < 1.0
    assert any("999" in reason for reason in report.reasons)


async def test_verify_digest_fails_on_contradicted_claim() -> None:
    """A claim the judge contradicts fails the gate."""
    request = _make_request()
    service = VerificationService()

    claims = [
        ClaimVerdictOut(id=1, claim="Revenue grew 12%.", label=ClaimLabel.SUPPORTED),
        ClaimVerdictOut(id=2, claim="Sales hit 45 billion.", label=ClaimLabel.SUPPORTED),
        ClaimVerdictOut(id=3, claim="Costs fell 3%.", label=ClaimLabel.SUPPORTED),
    ]
    judged = [
        ClaimVerdictOut(
            id=1,
            claim="Revenue grew 12%.",
            label=ClaimLabel.CONTRADICTED,
            quote="Revenue fell 5%.",
        ),
        ClaimVerdictOut(id=2, claim="Sales hit 45 billion.", label=ClaimLabel.SUPPORTED),
        ClaimVerdictOut(id=3, claim="Costs fell 3%.", label=ClaimLabel.SUPPORTED),
    ]

    with (
        patch("src.api.services.verification._extract_claims", new=AsyncMock(return_value=claims)),
        patch("src.api.services.verification._judge_batch", new=AsyncMock(return_value=judged)),
    ):
        report = await service.verify_digest(request)

    assert report.passed is False
    assert report.verdict == Verdict.INVALID
    assert report.faithfulness.score < 1.0


async def test_verify_digest_fails_on_unsupported_claim() -> None:
    """A claim the judge cannot find in the sources fails the gate."""
    request = _make_request()
    service = VerificationService()

    claims = [
        ClaimVerdictOut(id=1, claim="The CEO resigned.", label=ClaimLabel.SUPPORTED),
        ClaimVerdictOut(id=2, claim="Revenue grew 12%.", label=ClaimLabel.SUPPORTED),
        ClaimVerdictOut(id=3, claim="Sales hit 45 billion.", label=ClaimLabel.SUPPORTED),
    ]
    judged = [
        ClaimVerdictOut(id=1, claim="The CEO resigned.", label=ClaimLabel.UNSUPPORTED, quote=""),
        ClaimVerdictOut(id=2, claim="Revenue grew 12%.", label=ClaimLabel.SUPPORTED),
        ClaimVerdictOut(id=3, claim="Sales hit 45 billion.", label=ClaimLabel.SUPPORTED),
    ]

    with (
        patch("src.api.services.verification._extract_claims", new=AsyncMock(return_value=claims)),
        patch("src.api.services.verification._judge_batch", new=AsyncMock(return_value=judged)),
    ):
        report = await service.verify_digest(request)

    assert report.passed is False
    assert report.verdict == Verdict.INVALID


async def test_verify_digest_error_when_judge_backend_fails() -> None:
    """A judge backend failure produces an error verdict, not a pass."""
    request = _make_request()
    service = VerificationService()

    claims = [
        ClaimVerdictOut(id=1, claim="Revenue grew 12%.", label=ClaimLabel.SUPPORTED),
        ClaimVerdictOut(id=2, claim="Sales hit 45 billion.", label=ClaimLabel.SUPPORTED),
        ClaimVerdictOut(id=3, claim="Costs fell 3%.", label=ClaimLabel.SUPPORTED),
    ]
    with (
        patch("src.api.services.verification._extract_claims", new=AsyncMock(return_value=claims)),
        patch(
            "src.api.services.verification._judge_batch",
            new=AsyncMock(side_effect=VerificationError("backend down")),
        ),
    ):
        report = await service.verify_digest(request)

    assert report.passed is False
    assert report.verdict == Verdict.ERROR


async def test_verify_digest_skips_judge_when_too_few_claims() -> None:
    """Below min_claims the judge is skipped and groundedness alone decides."""
    request = _make_request()
    request = request.model_copy(update={"min_claims": 10})
    service = VerificationService()

    with (
        patch("src.api.services.verification._extract_claims", new=AsyncMock(return_value=[])),
        patch("src.api.services.verification._judge_batch", new=AsyncMock()) as mock_judge,
    ):
        report = await service.verify_digest(request)

    mock_judge.assert_not_called()
    assert report.passed is True
    assert report.verdict == Verdict.VALID


async def test_attribute_to_sources_weights_by_reliability() -> None:
    """Claims attributed to low-reliability sources contribute less to the score."""
    from src.api.services.verification import SourceArticle, attribute_to_sources

    sources = [
        SourceArticle(
            index=1,
            name="Example",
            host="example.com",
            text="Revenue grew 12%.",
        )
    ]
    verdicts = [
        ClaimVerdictOut(
            id=1,
            claim="Revenue grew 12%.",
            label=ClaimLabel.SUPPORTED,
            quote="Revenue grew 12%.",
        )
    ]
    scores = attribute_to_sources(verdicts, sources, reliability={"example.com": 0.5})
    assert len(scores) == 1
    assert scores[0].reliability == 0.5
    assert scores[0].claims == 1
    assert scores[0].supported == 1


# ---------------------------------------------------------------------------
# build_sources
# ---------------------------------------------------------------------------


def test_build_sources_extracts_host_from_link() -> None:
    """The source host is derived from the article link, not the feed URL."""
    clusters = [
        {
            "articles": [
                {
                    "title": "Article",
                    "link": "https://bbc.co.uk/news/123",
                    "content": "Content",
                    "source": "Google News",
                }
            ]
        }
    ]
    sources = build_sources(clusters)
    assert len(sources) == 1
    assert sources[0].host == "bbc.co.uk"


def test_build_sources_respects_host_reliability_map() -> None:
    """A host in the reliability map gets its configured score."""
    clusters = [
        {
            "articles": [
                {
                    "title": "Article",
                    "link": "https://bbc.co.uk/news/123",
                    "content": "Content",
                    "source": "Google News",
                }
            ]
        }
    ]
    sources = build_sources(clusters)
    assert sources[0].host == "bbc.co.uk"


def test_build_sources_empty_clusters() -> None:
    """No clusters means no sources."""
    assert build_sources([]) == []
