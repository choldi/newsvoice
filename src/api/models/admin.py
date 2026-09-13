"""Pydantic models for the admin GUI REST endpoints.

All responses follow the API convention of carrying a top-level `status` field
("ok" on success). Inputs use `In` suffixes; outputs use `Out`.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from src.core.collections import DEFAULT_COLLECTION_TIME, DEFAULT_PROMPT_TEMPLATE, CollectionConfig


class CollectionIn(BaseModel):
    """Input payload to create or update a collection."""

    name: str = Field(..., description="Unique collection identifier (e.g. 'tech')")
    feed_urls: list[str] = Field(
        default_factory=list, description="RSS feed URLs for this collection"
    )
    time: str = Field(
        default=DEFAULT_COLLECTION_TIME,
        description="Schedule time in HH:MM (UTC) for this collection's digest",
    )
    tts_enabled: bool = Field(default=False, description="Generate audio for this digest")
    deliver_audio: bool = Field(default=False, description="Deliver the generated audio")
    podcast_name: str = Field(default="", description="Podcast name injected into the LLM prompt")
    podcast_behavior: str = Field(
        default="", description="Podcaster behavior instruction (empty = off)"
    )
    language: str = Field(default="", description="ISO 639-1 language code for the digest")
    voice: str = Field(default="", description="OmniVoice voice override")
    prompt_template: str = Field(
        default=DEFAULT_PROMPT_TEMPLATE,
        description="Prompt template name from DIGEST_TEMPLATES",
    )
    chat_id: str | None = Field(
        default=None,
        description="Telegram chat id override; global TELEGRAM_CHAT_ID if empty",
    )
    enabled: bool = Field(default=True, description="Pause a collection without deleting it")

    def to_config(self) -> CollectionConfig:
        """Validate and convert the payload into a CollectionConfig.

        Returns:
            A validated CollectionConfig built from this payload.
        """
        return CollectionConfig.model_validate(self.model_dump())


class CollectionOut(BaseModel):
    """Response carrying a single collection."""

    status: str = Field(default="ok", description="Execution status: ok or error")
    collection: CollectionConfig = Field(..., description="The collection configuration")


class CollectionsListOut(BaseModel):
    """Response carrying the full list of collections."""

    status: str = Field(default="ok", description="Execution status: ok or error")
    collections: list[CollectionConfig] = Field(..., description="All collections")


class VoicesOut(BaseModel):
    """Response carrying a list of voices (stock or cloned)."""

    status: str = Field(default="ok", description="Execution status: ok or error")
    voices: list[dict[str, Any]] = Field(..., description="Voice entries from OmniVoice")


class VoiceCloneIn(BaseModel):
    """Input payload to clone a voice from a base64-encoded reference audio."""

    name: str = Field(..., description="Unique voice name (^[a-zA-Z0-9_-]+$)")
    language: str = Field(..., description="ISO 639-1 language code")
    audio_base64: str = Field(..., description="Base64-encoded reference audio (WAV/FLAC, 5-30s)")
    filename: str = Field(default="reference.wav", description="Original audio filename")


class VoiceCloneOut(BaseModel):
    """Response carrying the result of a voice clone request."""

    status: str = Field(default="ok", description="Execution status: ok or error")
    voice: dict[str, Any] = Field(..., description="The OmniVoice cloning response")


class RunOut(BaseModel):
    """Response carrying a single digest pipeline run."""

    status: str = Field(default="ok", description="Execution status: ok or error")
    run: dict[str, Any] = Field(..., description="Run row from digest_runs")


class RunsListOut(BaseModel):
    """Response carrying a filtered list of digest runs."""

    status: str = Field(default="ok", description="Execution status: ok or error")
    runs: list[dict[str, Any]] = Field(..., description="Run rows, newest first")
    total: int = Field(..., description="Matching run count (without pagination)")


class TriggerRunOut(BaseModel):
    """Response carrying the outcome of a manual run trigger."""

    status: str = Field(default="ok", description="Execution status: ok or error")
    workflow_id: str = Field(..., description="Temporal workflow id of the started run")
    collection: str = Field(..., description="Collection name that was triggered")


class ResyncOut(BaseModel):
    """Response carrying the schedule resync outcome."""

    status: str = Field(default="ok", description="Execution status: ok or error")
    created_or_updated: list[str] = Field(..., description="Schedule ids created or updated")
    removed: list[str] = Field(..., description="Schedule ids removed")
