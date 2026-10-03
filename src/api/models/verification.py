"""Verification models (Phase 7.13).

The verification of a generated digest produces two independent scores and one
verdict:

- ``groundedness`` is deterministic and free: every number, date, percentage and
  currency figure in the generated text must exist in a source article.
- ``faithfulness`` is the LLM judge's share of claims the sources actually back.

Both feed a single verdict, so a digest that cites a fabricated figure fails even
when the judge is lenient, and vice versa.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class VerificationTarget(StrEnum):
    """What a verification row is about."""

    DIGEST = "digest"
    NARRATION_SCRIPT = "narration_script"
    NARRATION_AUDIO = "narration_audio"


class VerificationMethod(StrEnum):
    """Which check produced a score."""

    GROUNDEDNESS = "groundedness"
    FAITHFULNESS = "faithfulness"
    ASR_SIMILARITY = "asr_similarity"


class Verdict(StrEnum):
    """Outcome of a verification."""

    VALID = "valid"
    INVALID = "invalid"
    ERROR = "error"


class ClaimLabel(StrEnum):
    """Judge verdict for a single atomic claim."""

    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    UNSUPPORTED = "unsupported"


class JudgeBackend(StrEnum):
    """Where the judging LLM runs."""

    LOCAL = "local"
    MODAL = "modal"
    API = "api"


class UngroundedNumberOut(BaseModel):
    """One figure of the generated text that no source article contains."""

    value: str = Field(..., description="Figure exactly as written in the generated text")
    kind: str = Field(default="number", description="number, percent, currency or date")
    snippet: str = Field(default="", description="Sentence the figure appears in")


class GroundednessOut(BaseModel):
    """Result of the deterministic figure check."""

    score: float = Field(default=1.0, ge=0.0, le=1.0, description="Grounded figures / total")
    total: int = Field(default=0, ge=0, description="Figures found in the generated text")
    grounded: int = Field(default=0, ge=0, description="Figures found verbatim in a source")
    ungrounded: list[UngroundedNumberOut] = Field(
        default_factory=list, description="Figures absent from every source"
    )


class ClaimVerdictOut(BaseModel):
    """Judge verdict for one atomic claim."""

    id: int = Field(..., description="Claim id as sent to the judge")
    claim: str = Field(..., description="The atomic claim text")
    label: ClaimLabel = Field(..., description="Judge verdict for the claim")
    quote: str = Field(default="", description="Verbatim source excerpt backing the verdict")


class FaithfulnessOut(BaseModel):
    """Result of the LLM judge pass."""

    score: float = Field(default=1.0, ge=0.0, le=1.0, description="Supported claims / total")
    total: int = Field(default=0, ge=0, description="Claims judged")
    supported: int = Field(default=0, ge=0)
    contradicted: int = Field(default=0, ge=0)
    unsupported: int = Field(default=0, ge=0)
    judged: bool = Field(
        default=False, description="False when groundedness alone decided the verdict"
    )
    model: str = Field(default="", description="Model that judged the claims")
    claims: list[ClaimVerdictOut] = Field(
        default_factory=list, description="Per-claim verdicts, unsupported ones included"
    )


class SourceScoreOut(BaseModel):
    """Per-source faithfulness, used to weigh the aggregate score."""

    source: str = Field(default="", description="Source name or host the claims came from")
    reliability: float = Field(default=1.0, ge=0.0, le=1.0, description="Configured reliability")
    claims: int = Field(default=0, ge=0, description="Claims attributed to this source")
    supported: int = Field(default=0, ge=0, description="Of those, claims the judge backed")


class VerificationReportOut(BaseModel):
    """Full verification verdict for one generated text."""

    status: str = Field(default="ok", description="Execution status: ok or error")
    verdict: Verdict = Field(default=Verdict.ERROR, description="Overall verdict")
    passed: bool = Field(default=False, description="True when the verdict is valid")
    target: VerificationTarget = Field(
        default=VerificationTarget.DIGEST, description="What was verified"
    )
    score: float = Field(default=0.0, ge=0.0, le=1.0, description="Weighted aggregate score")
    threshold: float = Field(default=0.0, ge=0.0, le=1.0, description="Applied threshold")
    groundedness: GroundednessOut = Field(default_factory=GroundednessOut)
    faithfulness: FaithfulnessOut = Field(default_factory=FaithfulnessOut)
    sources: list[SourceScoreOut] = Field(
        default_factory=list, description="Per-source reliability and claim counts"
    )
    reasons: list[str] = Field(
        default_factory=list, description="Human-readable reasons the verdict failed"
    )
    duration_seconds: float = Field(default=0.0, ge=0.0, description="Wall-clock time")


class VerifyDigestIn(BaseModel):
    """Input of the digest verification activity."""

    digest: str = Field(..., min_length=1, description="Generated digest markdown to verify")
    clusters: list[dict] = Field(
        default_factory=list, description="Story clusters the digest was built from"
    )
    collection: str = Field(default="default", description="Collection name")
    run_id: str = Field(default="", description="Digest workflow id")
    threshold: float = Field(default=0.9, ge=0.0, le=1.0, description="Faithfulness threshold")
    groundedness_threshold: float = Field(
        default=1.0, ge=0.0, le=1.0, description="Grounded figures threshold"
    )
    backend: str = Field(default="local", description="Judge backend: local, modal or api")
    model: str = Field(default="", description="Judge model override")
    source_reliability: dict[str, float] = Field(
        default_factory=dict, description="Host to reliability map"
    )


class VerificationsListOut(BaseModel):
    """Verification rows of a run for the admin API and the GUI."""

    status: str = Field(default="ok", description="Execution status: ok or error")
    run_id: str = Field(..., description="Digest run the rows belong to")
    verifications: list[dict] = Field(default_factory=list, description="Verification rows")
