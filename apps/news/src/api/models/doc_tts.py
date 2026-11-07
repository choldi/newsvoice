"""Pydantic models for the long-document TTS endpoints (Phase 7.11)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class DocTTSSubmitOut(BaseModel):
    """Response payload of POST /api/admin/documents/tts (202 Accepted)."""

    status: str = Field(default="ok", description="Execution status: ok or error")
    workflow_id: str = Field(..., description="Temporal workflow id of the job")
    title: str = Field(..., description="Title that will label the audio file")
    filename: str = Field(..., description="Original uploaded filename")
    status_url: str = Field(..., description="Path to poll for the job status")


class DocTTSJobOut(BaseModel):
    """Response payload of GET /api/admin/documents/tts/{workflow_id}."""

    status: str = Field(default="ok", description="Execution status: ok or error")
    workflow_id: str = Field(..., description="Temporal workflow id of the job")
    job_status: str = Field(
        ...,
        description=(
            "STARTING, GENERATING, PAUSED (yielded the engine to a scheduled "
            "digest), FINISHED_OK or FINISHED_ERROR"
        ),
    )
    title: str = Field(default="", description="Document title")
    filename: str = Field(default="", description="Original uploaded filename")
    kind: str = Field(default="", description="Detected document kind: pdf, epub, txt, md")
    char_count: int = Field(default=0, description="Extracted text length in characters")
    voice: str = Field(default="", description="Resolved Edge voice ShortName")
    language: str = Field(default="", description="ISO 639-1 language of the document")
    duration_estimate: float | None = Field(
        default=None, description="Estimated audio duration in seconds"
    )
    estimate_wall_seconds: float | None = Field(
        default=None,
        description=(
            "Phase 8.5: estimated wall-clock seconds the whole synthesis needs "
            "(0 when the audio is already cached). Written by the extraction "
            "step, before any audio exists; the submit response cannot carry it "
            "because the file has not been read yet."
        ),
    )
    eta_seconds: float | None = Field(
        default=None,
        description=(
            "Phase 8.5: seconds remaining, extrapolated from the measured "
            "rate of the chunks already finished. Null before the first chunk."
        ),
    )
    chunks_done: int = Field(default=0, description="Chunks synthesized so far")
    chunks_total: int = Field(default=0, description="Chunks the document split into")
    paused: bool = Field(
        default=False,
        description="True while the render has yielded the engine to a scheduled digest",
    )
    pause_reason: str | None = Field(
        default=None,
        description=(
            "Why the job is paused: 'operator' (manual pause) or 'lease' "
            "(handed the engine to a scheduled digest); null otherwise"
        ),
    )
    chunk_count: int = Field(default=0, description="Number of synthesized chunks")
    audio_url: str | None = Field(
        default=None,
        description="HMAC-signed link to the MP3; set only when FINISHED_OK",
    )
    error: str | None = Field(default=None, description="Failure detail when any")
    elapsed_seconds: float | None = Field(default=None, description="Synthesis wall time")
