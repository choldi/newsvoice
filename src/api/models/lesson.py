"""Pydantic models for the language-teacher endpoints (Phase 7.12)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from src.api.models.narration import NarrationSegment


class LessonMode(StrEnum):
    """How the class is taught and how many voices carry it."""

    MIXED = "mixed"
    """One voice: explains natively and weaves in target-language examples."""
    DUAL = "dual"
    """Two voices: the first speaks the native language, the second the target."""
    NATIVE = "native"
    """Everything explained in the native language, target phrases quoted."""


class LessonContent(BaseModel):
    """What the LLM produced for one lesson: readable text plus a spoken script.

    Attributes:
        title: Short lesson title (used for the attachment and the file name).
        markdown: The study document handed to the student as an attachment.
        segments: The spoken turns to synthesize, roles already named.
    """

    title: str = Field(default="", max_length=200, description="Lesson title")
    markdown: str = Field(..., min_length=1, description="Study document in Markdown")
    segments: list[NarrationSegment] = Field(
        ..., min_length=1, max_length=200, description="Spoken turns of the class"
    )


class LessonSubmitOut(BaseModel):
    """Response payload of POST /api/admin/lessons (202 Accepted)."""

    status: str = Field(default="ok", description="Execution status: ok or error")
    workflow_id: str = Field(..., description="Temporal workflow id of the lesson")
    collection: str = Field(..., description="Collection whose channels are used")
    title: str = Field(..., description="Lesson title (from the request)")
    mode: str = Field(..., description="Lesson mode: mixed, dual or native")
    status_url: str = Field(..., description="Path to poll for the lesson status")


class LessonJobOut(BaseModel):
    """Response payload of GET /api/admin/lessons/{workflow_id}."""

    status: str = Field(default="ok", description="Execution status: ok or error")
    workflow_id: str = Field(..., description="Temporal workflow id of the lesson")
    job_status: str = Field(..., description="STARTING, GENERATING, FINISHED_OK or FINISHED_ERROR")
    title: str = Field(default="", description="Lesson title")
    mode: str = Field(default="", description="Lesson mode")
    target_language: str = Field(default="", description="Language being taught")
    native_language: str = Field(default="", description="Student's native language")
    char_count: int = Field(default=0, description="Lesson text length in characters")
    duration_estimate: float | None = Field(
        default=None, description="Measured audio duration in seconds"
    )
    audio_url: str | None = Field(
        default=None,
        description="HMAC-signed link to the lesson audio; set when finished",
    )
    text_path: str = Field(default="", description="Path of the lesson .md attachment")
    error: str | None = Field(default=None, description="Failure detail when any")
    elapsed_seconds: float | None = Field(default=None, description="Render wall time")
