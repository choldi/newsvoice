"""LLM output verification (Phase 7.13).

Two independent checks decide whether a generated digest may be delivered:

1. :func:`groundedness` is deterministic and free. Every figure the generated text
   states (numbers, percentages, currency amounts, dates) must exist in one of the
   source articles. This is the check that catches the classic LLM failure --
   inventing a plausible statistic -- and it needs no model at all.
2. :func:`faithfulness` asks an LLM judge to decompose the generated text into
   atomic claims and label each one ``supported``, ``contradicted`` or
   ``unsupported`` against the sources, quoting the evidence.

The judge backend is configurable: ``local`` (the pipeline's own LLM, free),
``modal`` (a bigger judge on Modal, needs credentials) or ``api`` (a hosted
provider). A 4B local model is a weak judge, so
``llm_verify_threshold`` must be calibrated against known-good digests before it
is trusted; see ``Deploy.md``.

Neither check ever repairs the text: the verdict is binary. When the score misses
the threshold the caller is expected to fail the run instead of delivering.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import structlog

from src.api.models.verification import (
    ClaimLabel,
    ClaimVerdictOut,
    FaithfulnessOut,
    GroundednessOut,
    JudgeBackend,
    SourceScoreOut,
    UngroundedNumberOut,
    Verdict,
    VerificationReportOut,
    VerificationTarget,
    VerifyDigestIn,
)
from src.core.budget import preflight
from src.core.config import settings
from src.core.llm import generate_completion
from src.core.prompts import (
    VERIFY_CLAIM_SYSTEM_PROMPT,
    VERIFY_CLAIM_USER_PROMPT_TEMPLATE,
    VERIFY_CLAIMS_SYSTEM_PROMPT,
    VERIFY_CLAIMS_USER_PROMPT_TEMPLATE,
)
from src.core.tracing import trace_function

logger = structlog.get_logger()

JUDGE_TEMPERATURE = 0.0

# Figures worth grounding. Bare integers included, but only as whole tokens: a
# substring match would let "10" pass inside "210".
# A bare figure only counts when it is not glued to a word: "6" inside "GPT-6" or
# "70B" inside "Llama-3-70B" is part of a product name, not a claim about a
# quantity. Percentages and currency may sit next to a symbol, so they are
# matched separately and keep their own boundaries.
_NUMBER_RE = re.compile(r"(?<![\w-])\d+(?:[.,]\d+)*(?![\w-])")
_CURRENCY_RE = re.compile(r"(?<![\w-])[€$£]\s?\d+(?:[.,]\d+)*(?![\w-])")
_PERCENT_RE = re.compile(r"(?<![\w-])\d+(?:[.,]\d+)*\s?%(?![\w-])")
_DATE_RE = re.compile(
    r"(?<![\w-])\d{4}-\d{2}-\d{2}(?![\w-])|(?<![\w-])\d{1,2}/\d{1,2}/\d{2,4}(?![\w-])"
    r"|(?<![\w-])\d{1,2}\.\d{1,2}\.\d{2,4}(?![\w-])"
)
# Markdown scaffolding of the digest format carries numbers that are not claims:
# "## Main Stories" is structure, and "1." opening a list item is an index. Both
# are removed before figures are read, otherwise every digest fails on its own
# layout.
_MARKDOWN_STRUCTURE_RE = re.compile(
    r"^\s{0,3}#{1,6}\s*"
    r"|^\s{0,3}\d+[.)]\s+"
    r"|^\s{0,3}[-*+]\s+"
    r"|\*\*\d+\.\*\*"
    r"|\*\*([^*]+)\*\*",
    re.MULTILINE,
)
# Words that introduce a magnitude; the LLM says "12 million" where the source may
# say "12M", so only the numeric part is grounded.
_MAGNITUDES = (
    "trillion",
    "trillones",
    "billion",
    "billions",
    "millones",
    "mil millones",
    "million",
    "millions",
    "billones",
    "miles",
    "mil",
)
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")
# A capitalized word immediately before a figure marks a product or version name.
_PRODUCT_NAME_RE = re.compile(r"(?:^|[\s(\"'])[A-Z][A-Za-z0-9]{1,}\s+$")


class VerificationError(RuntimeError):
    """Raised when a verification could not be completed.

    Distinct from a low score: this is an infrastructure failure (judge
    unreachable, unparsable reply), not evidence that the text is unfaithful. The
    caller reports it as ``Verdict.ERROR`` so an operator can tell the two apart.
    """


@dataclass(frozen=True)
class SourceArticle:
    """One source article offered to the judge and to the groundedness check."""

    index: int
    name: str
    host: str
    text: str


def normalize_for_match(text: str) -> str:
    """Normalize text for figure matching.

    Lowercases, strips thousands separators and markdown emphasis so that
    "1.200", "1,200" and "1200" compare equal, and collapses whitespace.

    Args:
        text: Raw text.

    Returns:
        Normalized single-line text.
    """
    lowered = text.lower()
    # European thousands dot: "1.200" -> "1200". A single dot between two groups
    # of three digits is a separator, not a decimal point.
    lowered = re.sub(r"(?<=\d)\.(?=\d{3}(?!\d))", "", lowered)
    lowered = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", lowered)
    lowered = re.sub(r"[*_`#>]", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def _figure_forms(figure: str) -> list[str]:
    """Return every plausible textual form of a figure.

    A Spanish source writes "1.200 euros" where an English one writes "1,200", and
    "3,5" and "3.5" both occur. Rather than guess the locale, accept any of the
    forms.

    Args:
        figure: Figure as written in the generated text.

    Returns:
        Normalized forms to look for in the source text.
    """
    digits = re.sub(r"[^\d.,]", "", figure)
    if not digits:
        return []
    # The figure as written must be a candidate: "2026-10-02" appears in the source
    # with its separators, and stripping them would make it unfindable.
    forms = {figure, digits}
    if "," in digits:
        forms.add(digits.replace(",", "."))
        forms.add(digits.replace(",", ""))
    if "." in digits:
        forms.add(digits.replace(".", ","))
        forms.add(digits.replace(".", ""))
    return [normalize_for_match(form) for form in forms]


def _is_grounded(figure: str, haystack: str) -> bool:
    """Check whether a figure appears in normalized source text.

    Args:
        figure: Figure as written in the generated text.
        haystack: Normalized concatenation of every source article.

    Returns:
        True when any plausible form of the figure appears as a whole token.
    """
    for form in _figure_forms(figure):
        if not form:
            continue
        if re.search(rf"(?<!\d){re.escape(form)}(?!\d)", haystack):
            return True
    return False


def _strip_magnitude(text: str) -> str:
    """Remove magnitude words so only the numeric part is matched.

    Args:
        text: Text possibly containing a magnitude word.

    Returns:
        The text without trailing magnitude words.
    """
    pattern = "|".join(re.escape(word) for word in _MAGNITUDES)
    return re.sub(rf"\s*(?:{pattern})\b", " ", text, flags=re.IGNORECASE)


def _sentence_of(text: str, start: int, end: int) -> str:
    """Return the sentence surrounding a match.

    Args:
        text: Original text.
        start: Match start offset.
        end: Match end offset.

    Returns:
        The sentence, truncated to a readable length.
    """
    left = max((m.end() for m in _SENTENCE_RE.finditer(text, 0, start)), default=0)
    candidates = [pos for pos in (text.find(".", end), text.find("\n", end)) if pos != -1]
    right = min(candidates) if candidates else len(text)
    snippet = text[left:right].strip()
    return snippet[:200]


def strip_markdown_structure(text: str) -> str:
    """Remove the digest's markdown scaffolding, preserving the prose.

    Headings, list markers and bold item indexes carry digits that belong to the
    layout rather than to the news, so they must not be grounded.

    Args:
        text: Raw digest markdown.

    Returns:
        The text with structural tokens removed and line structure preserved.
    """
    cleaned = _MARKDOWN_STRUCTURE_RE.sub(lambda m: m.group(1) or "", text)
    # The stripped markers leave leading whitespace behind; keep one space so
    # words do not get glued together.
    return re.sub(r"[ \t]{2,}", " ", cleaned)


def extract_figures(text: str) -> list[tuple[str, str]]:
    """Extract every checkable figure from generated text.

    Args:
        text: Generated digest markdown.

    Returns:
        List of ``(figure, kind)`` tuples with kind in 'currency', 'percent',
        'date' or 'number', in order of appearance and without duplicates. Richer
        spellings win: a percentage is reported as a percentage, not also as the
        bare number inside it.
    """
    prose = strip_markdown_structure(text)
    spans: list[tuple[int, int, str, str]] = []
    bare_numbers: list[tuple[int, int, str]] = []
    for pattern, kind in (
        (_CURRENCY_RE, "currency"),
        (_PERCENT_RE, "percent"),
        (_DATE_RE, "date"),
    ):
        for match in pattern.finditer(prose):
            spans.append((match.start(), match.end(), match.group(0), kind))
    # Magnitude words are dropped before reading bare numbers so that "45 billion"
    # and "45" share one span, but the offset is restored to keep the sentence
    # lookup pointing at the original text.
    stripped = _strip_magnitude(prose)
    offset = len(prose) - len(stripped)
    for match in _NUMBER_RE.finditer(stripped):
        bare_numbers.append((match.start() + offset, match.end() + offset, match.group(0)))
    for start, end, value in bare_numbers:
        # "Llama 3.3" and "GPT 5" are product names, not quantities. The figure is
        # dropped when a capitalized word sits right before it: failing a digest
        # over a model version would make the check useless.
        if _PRODUCT_NAME_RE.search(prose, max(start - 40, 0), start):
            continue
        spans.append((start, end, value, "number"))
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    claimed: list[tuple[int, int]] = []
    for start, end, value, kind in sorted(spans, key=lambda item: (item[0], -item[1])):
        # A date contains three numbers and a percentage contains one; only the
        # outermost span counts, so overlapping matches are skipped.
        if any(start < taken_end and end > taken_start for taken_start, taken_end in claimed):
            continue
        key = normalize_for_match(re.sub(r"[^\d.,]", "", value))
        if not key or key in seen:
            continue
        seen.add(key)
        claimed.append((start, end))
        found.append((value.strip(), kind))
    return found


def groundedness(digest: str, sources: list[SourceArticle]) -> GroundednessOut:
    """Check every figure of the digest against the source articles.

    Args:
        digest: Generated digest markdown.
        sources: Source articles to ground the figures in.

    Returns:
        The score, the counts and the list of figures no source contains.
    """
    haystack = normalize_for_match(" ".join(source.text for source in sources))
    prose = strip_markdown_structure(digest)
    figures = extract_figures(digest)
    if not figures:
        return GroundednessOut(score=1.0, total=0, grounded=0, ungrounded=[])
    ungrounded: list[UngroundedNumberOut] = []
    grounded = 0
    for value, kind in figures:
        if _is_grounded(value, haystack):
            grounded += 1
            continue
        position = max(prose.find(value), 0)
        ungrounded.append(
            UngroundedNumberOut(
                value=value,
                kind=kind,
                snippet=_sentence_of(prose, position, position + len(value)),
            )
        )
    total = len(figures)
    return GroundednessOut(
        score=grounded / total,
        total=total,
        grounded=grounded,
        ungrounded=ungrounded,
    )


def _parse_json_object(content: str) -> dict:
    """Parse a JSON object out of an LLM reply, tolerating code fences.

    Args:
        content: Raw reply text.

    Returns:
        The decoded object.

    Raises:
        VerificationError: When no JSON object can be decoded.
    """
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        raise VerificationError(f"judge reply is not JSON: {content[:200]}")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise VerificationError(f"judge returned invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise VerificationError("judge reply is not a JSON object")
    return data


class LlmJudge:
    """Judge that labels claims, on the local LLM, on Modal or on a hosted API."""

    def __init__(
        self,
        backend: str | None = None,
        model: str | None = None,
    ) -> None:
        """Initialize the judge.

        Args:
            backend: 'local', 'modal' or 'api'. Defaults to the configured one.
            model: Judge model override. Defaults to the configured one.
        """
        raw = (backend or settings.llm_verify_backend or JudgeBackend.LOCAL).strip().lower()
        try:
            self.backend = JudgeBackend(raw)
        except ValueError as exc:
            raise VerificationError(
                f"unknown judge backend {raw!r}: expected local, modal or api"
            ) from exc
        self.model = (model or settings.llm_verify_model or "").strip() or self._default_model()

    def _default_model(self) -> str:
        """Return the model implied by the backend when none is configured.

        Returns:
            The configured model, the pipeline model for the local backend or the
            Modal default.
        """
        if self.backend is JudgeBackend.MODAL:
            return settings.modal_judge_model
        return settings.llm_model

    def describe(self) -> str:
        """Return a human-readable backend/model pair for the report.

        Returns:
            String like 'local:Qwen3.5-4B'.
        """
        return f"{self.backend.value}:{self.model}"

    async def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        run_id: str = "",
        collection: str = "default",
    ) -> dict:
        """Run one judge call and return its decoded JSON object.

        Args:
            system_prompt: Judge system prompt.
            user_prompt: Judge user prompt.
            run_id: Digest run id, for the Modal spend ledger.
            collection: Collection name, for the Modal spend ledger.

        Returns:
            The decoded JSON object.

        Raises:
            VerificationError: When the call fails or the reply is not JSON.
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        if self.backend is JudgeBackend.MODAL:
            return await self._complete_modal(
                system_prompt, user_prompt, run_id=run_id, collection=collection
            )
        try:
            content = await generate_completion(
                messages=messages,
                temperature=JUDGE_TEMPERATURE,
                model=self.model,
                api_base=settings.llm_verify_api_base if self.backend is JudgeBackend.API else None,
                api_key=settings.llm_verify_api_key if self.backend is JudgeBackend.API else None,
                response_format="json_object",
            )
        except RuntimeError as exc:
            raise VerificationError(f"judge call failed: {exc}") from exc
        return _parse_json_object(content)

    async def _complete_modal(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        run_id: str,
        collection: str,
    ) -> dict:
        """Run one judge call on Modal, behind the spend guard.

        Args:
            system_prompt: Judge system prompt.
            user_prompt: Judge user prompt.
            run_id: Digest run id, recorded in the spend ledger.
            collection: Collection name, recorded in the spend ledger.

        Returns:
            The decoded JSON object.

        Raises:
            VerificationError: When Modal is unusable, the budget blocks the call
                or the remote function fails.
        """
        from src.core.modal_client import modal_judge
        from src.core.modal_cost import set_cost_context

        if not settings.modal_enabled:
            raise VerificationError("judge backend 'modal' requires modal_enabled=true")
        if not settings.modal_token_id or not settings.modal_token_secret:
            raise VerificationError("judge backend 'modal' has no Modal credentials")
        prompt_chars = len(system_prompt) + len(user_prompt)
        decision = await preflight(char_count=prompt_chars, cache_hit=False)
        if not decision.allowed:
            raise VerificationError(f"modal budget guard blocked the judge: {decision.reason}")
        # The Modal client measures and records the real cost of this call; the
        # context only labels the ledger row.
        set_cost_context(collection=collection, run_id=run_id or "", stage="llm_verify")
        try:
            payload = await modal_judge(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=self.model,
                max_tokens=settings.llm_max_tokens,
            )
        except Exception as exc:
            raise VerificationError(f"modal judge call failed: {exc}") from exc
        content = payload.get("content") if isinstance(payload, dict) else None
        if not content:
            raise VerificationError("modal judge returned no content")
        return _parse_json_object(str(content))


def build_sources(clusters: list[dict], *, max_chars: int | None = None) -> list[SourceArticle]:
    """Flatten story clusters into source articles.

    Args:
        clusters: Cluster dicts as returned by ``cluster_activity``.
        max_chars: Optional per-article character cap. ``None`` keeps the full
            text, which is what the groundedness check wants.

    Returns:
        The sources, numbered in cluster order.
    """
    limit = max_chars if max_chars is not None else 0
    sources: list[SourceArticle] = []
    for cluster in clusters:
        articles = cluster.get("articles") or []
        for article in articles:
            if not isinstance(article, dict):
                continue
            link = str(article.get("link") or "")
            host = urlparse(link).hostname or ""
            content = str(article.get("content") or "")
            if limit and len(content) > limit:
                content = f"{content[:limit]}..."
            text = "\n".join(part for part in (str(article.get("title") or ""), content) if part)
            if not text.strip():
                continue
            sources.append(
                SourceArticle(
                    index=len(sources) + 1,
                    name=str(article.get("source") or host or "unknown"),
                    host=host,
                    text=text,
                )
            )
    return sources


def _format_sources(sources: list[SourceArticle]) -> str:
    """Render sources for the judge prompt.

    Args:
        sources: Sources to render.

    Returns:
        Numbered source blocks.
    """
    return "\n\n".join(f"[{source.index}] {source.text}" for source in sources)


async def _extract_claims(
    digest: str, judge: LlmJudge, *, run_id: str, collection: str
) -> list[str]:
    """Decompose the generated digest into atomic claims.

    Args:
        digest: Generated digest markdown.
        judge: Judge to ask.
        run_id: Digest run id, for the spend ledger.
        collection: Collection name, for the spend ledger.

    Returns:
        The claims, in order.

    Raises:
        VerificationError: When the judge reply carries no claims array.
    """
    data = await judge.complete_json(
        VERIFY_CLAIMS_SYSTEM_PROMPT,
        VERIFY_CLAIMS_USER_PROMPT_TEMPLATE.format(digest=digest),
        run_id=run_id,
        collection=collection,
    )
    claims = data.get("claims")
    if not isinstance(claims, list):
        raise VerificationError("claim extraction returned no 'claims' array")
    return [str(claim).strip() for claim in claims if str(claim).strip()]


async def _judge_batch(
    claims: list[tuple[int, str]],
    sources: list[SourceArticle],
    judge: LlmJudge,
    *,
    run_id: str,
    collection: str,
) -> list[ClaimVerdictOut]:
    """Judge one batch of claims.

    Args:
        claims: ``(id, text)`` pairs to judge.
        sources: Sources offered as evidence.
        judge: Judge to ask.
        run_id: Digest run id, for the spend ledger.
        collection: Collection name, for the spend ledger.

    Returns:
        One verdict per claim, in the order the claims were sent.

    Raises:
        VerificationError: When the judge skips a claim: a partial reply is not
            trustworthy, so the caller must not turn it into a low score.
    """
    numbered = "\n".join(f"{claim_id}. {text}" for claim_id, text in claims)
    data = await judge.complete_json(
        VERIFY_CLAIM_SYSTEM_PROMPT,
        VERIFY_CLAIM_USER_PROMPT_TEMPLATE.format(
            source_count=len(sources),
            sources_text=_format_sources(sources),
            claims_text=numbered,
        ),
        run_id=run_id,
        collection=collection,
    )
    verdicts = data.get("verdicts")
    if not isinstance(verdicts, list):
        raise VerificationError("judge returned no 'verdicts' array")
    by_id: dict[int, tuple[ClaimLabel, str]] = {}
    for entry in verdicts:
        if not isinstance(entry, dict):
            continue
        try:
            claim_id = int(entry.get("id"))
        except (TypeError, ValueError):
            continue
        raw_label = str(entry.get("label") or "").strip().lower()
        try:
            label = ClaimLabel(raw_label)
        except ValueError:
            logger.warning("verification_unknown_label", label=raw_label, claim_id=claim_id)
            label = ClaimLabel.UNSUPPORTED
        by_id[claim_id] = (label, str(entry.get("quote") or "").strip())
    missing = [claim_id for claim_id, _ in claims if claim_id not in by_id]
    if missing:
        raise VerificationError(f"judge skipped claims {missing}")
    return [
        ClaimVerdictOut(
            id=claim_id,
            claim=text,
            label=by_id[claim_id][0],
            quote=by_id[claim_id][1],
        )
        for claim_id, text in claims
    ]


async def faithfulness(
    digest: str,
    sources: list[SourceArticle],
    judge: LlmJudge,
    *,
    run_id: str = "",
    collection: str = "default",
) -> FaithfulnessOut:
    """Score how much of the generated digest the sources actually back.

    Args:
        digest: Generated digest markdown.
        sources: Sources offered as evidence.
        judge: Judge to ask.
        run_id: Digest run id, for the spend ledger.
        collection: Collection name, for the spend ledger.

    Returns:
        The score, the label counts and every per-claim verdict.

    Raises:
        VerificationError: When the judge fails or answers incompletely.
    """
    claims = await _extract_claims(digest, judge, run_id=run_id, collection=collection)
    total_wanted = min(len(claims), settings.llm_verify_max_claims)
    claims = claims[:total_wanted]
    if len(claims) < settings.llm_verify_min_claims:
        logger.info(
            "verification_too_few_claims",
            claims=len(claims),
            minimum=settings.llm_verify_min_claims,
        )
        return FaithfulnessOut(judged=False, model=judge.describe())
    verdicts: list[ClaimVerdictOut] = []
    per_call = max(1, settings.llm_verify_claims_per_call)
    for start in range(0, len(claims), per_call):
        batch = list(enumerate(claims[start : start + per_call], start=start + 1))
        verdicts.extend(
            await _judge_batch(batch, sources, judge, run_id=run_id, collection=collection)
        )
    supported = sum(1 for verdict in verdicts if verdict.label is ClaimLabel.SUPPORTED)
    contradicted = sum(1 for verdict in verdicts if verdict.label is ClaimLabel.CONTRADICTED)
    unsupported = sum(1 for verdict in verdicts if verdict.label is ClaimLabel.UNSUPPORTED)
    total = len(verdicts)
    return FaithfulnessOut(
        score=supported / total if total else 0.0,
        total=total,
        supported=supported,
        contradicted=contradicted,
        unsupported=unsupported,
        judged=True,
        model=judge.describe(),
        claims=verdicts,
    )


def attribute_to_sources(
    verdicts: list[ClaimVerdictOut],
    sources: list[SourceArticle],
    reliability: dict[str, float],
) -> list[SourceScoreOut]:
    """Attribute judged claims to the source that backs them.

    The judge's quote is the attribution key: whichever source article contains it
    verbatim gets the credit, and its configured reliability weights the
    aggregate. Claims the judge could not support have no quote and are counted
    under 'unattributed'.

    Args:
        verdicts: Per-claim verdicts.
        sources: Sources offered as evidence.
        reliability: Host to reliability map.

    Returns:
        Per-source claim counts, sorted by claim count descending.
    """
    default = settings.source_reliability_default
    normalized = {host.lower(): value for host, value in reliability.items()}
    per_source: dict[str, SourceScoreOut] = {}

    def slot(name: str, host: str) -> SourceScoreOut:
        if name not in per_source:
            per_source[name] = SourceScoreOut(
                source=name,
                reliability=normalized.get(host.lower(), default) if host else default,
            )
        return per_source[name]

    prepared = [normalize_for_match(source.text) for source in sources]
    for verdict in verdicts:
        quote = normalize_for_match(verdict.quote)
        name, host = "unattributed", ""
        if quote:
            for source, haystack in zip(sources, prepared, strict=True):
                if quote in haystack:
                    name, host = source.name, source.host
                    break
        entry = slot(name, host)
        entry.claims += 1
        if verdict.label is ClaimLabel.SUPPORTED:
            entry.supported += 1
    return sorted(per_source.values(), key=lambda item: (-item.claims, item.source))


def _weighted_score(faith: FaithfulnessOut, sources: list[SourceScoreOut]) -> float:
    """Return the reliability-weighted support rate.

    Args:
        faith: Faithfulness result.
        sources: Per-source claim counts.

    Returns:
        The weighted score, or the plain support rate when nothing could be
        attributed to a source.
    """
    weight_total = sum(entry.reliability * entry.claims for entry in sources)
    if weight_total <= 0:
        return faith.score
    supported = sum(entry.reliability * entry.supported for entry in sources)
    return supported / weight_total


class VerificationService:
    """Verifies a generated digest against its sources and decides delivery."""

    def __init__(self, judge: LlmJudge | None = None) -> None:
        """Initialize the service.

        Args:
            judge: Judge to use. Defaults to a judge built from the settings.
        """
        self._judge = judge

    def judge(self, backend: str | None = None, model: str | None = None) -> LlmJudge:
        """Return the judge for a run.

        Args:
            backend: Optional backend override.
            model: Optional model override.

        Returns:
            The injected judge when present, otherwise a new one.
        """
        if self._judge is not None:
            return self._judge
        return LlmJudge(backend=backend, model=model)

    @trace_function(component="api.services.verification")
    async def verify_digest(self, payload: VerifyDigestIn) -> VerificationReportOut:
        """Verify a digest and return its verdict.

        The groundedness check runs first and never needs a model. The judge is
        only asked when the digest carries enough claims for a score to mean
        something.

        Args:
            payload: Digest, sources and thresholds.

        Returns:
            The full report. A judge failure yields ``Verdict.ERROR`` rather than a
            low score, so an operator can tell "the text is unfaithful" from "the
            judge did not answer".
        """
        started = time.monotonic()
        sources = build_sources(payload.clusters)
        if not sources:
            return VerificationReportOut(
                verdict=Verdict.ERROR,
                threshold=payload.threshold,
                reasons=["no source articles to verify against"],
                duration_seconds=round(time.monotonic() - started, 2),
            )
        ground = groundedness(payload.digest, sources)
        judge = self.judge(payload.backend, payload.model)
        reasons: list[str] = []
        if ground.score < payload.groundedness_threshold:
            reasons.append(
                f"groundedness {ground.score:.2f} < {payload.groundedness_threshold:.2f}: "
                + ", ".join(f"{item.value} ({item.kind})" for item in ground.ungrounded[:5])
            )
        faith = FaithfulnessOut(model=judge.describe())
        try:
            judge_sources = build_sources(
                payload.clusters, max_chars=settings.llm_verify_source_chars
            )[: settings.llm_verify_max_sources]
            faith = await faithfulness(
                payload.digest,
                judge_sources,
                judge,
                run_id=payload.run_id,
                collection=payload.collection,
            )
        except VerificationError as exc:
            if not reasons:
                return VerificationReportOut(
                    verdict=Verdict.ERROR,
                    target=VerificationTarget.DIGEST,
                    groundedness=ground,
                    faithfulness=faith,
                    threshold=payload.threshold,
                    reasons=[f"judge error: {exc}"],
                    duration_seconds=round(time.monotonic() - started, 2),
                )
            reasons.append(f"judge error: {exc}")
        source_scores = attribute_to_sources(faith.claims, sources, payload.source_reliability)
        score = faith.score
        if faith.judged:
            score = _weighted_score(faith, source_scores)
            if score < payload.threshold:
                reasons.append(
                    f"faithfulness {score:.2f} < {payload.threshold:.2f} "
                    f"({faith.supported}/{faith.total} supported, "
                    f"{faith.contradicted} contradicted, {faith.unsupported} unsupported)"
                )
        verdict = Verdict.VALID if not reasons else Verdict.INVALID
        return VerificationReportOut(
            verdict=verdict,
            passed=verdict is Verdict.VALID,
            target=VerificationTarget.DIGEST,
            score=round(score, 4),
            threshold=payload.threshold,
            groundedness=ground,
            faithfulness=faith,
            sources=source_scores,
            reasons=reasons,
            duration_seconds=round(time.monotonic() - started, 2),
        )
