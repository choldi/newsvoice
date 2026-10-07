"""Pydantic models for the admin GUI REST endpoints.

All responses follow the API convention of carrying a top-level `status` field
("ok" on success). Inputs use `In` suffixes; outputs use `Out`.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.api.models.narration import NarrationSpec
from src.core.collections import (
    DEFAULT_COLLECTION_TIME,
    DEFAULT_PROMPT_TEMPLATE,
    CollectionConfig,
    LogicDeliveryConfig,
)


class CollectionIn(BaseModel):
    """Input payload to create or update a collection.

    Mirrors every `CollectionConfig` field so the admin GUI and REST API can
    round-trip a collection without dropping configuration. Unknown fields are
    rejected (422) instead of being silently ignored, so a new canonical field
    cannot disappear on the next GUI save.
    """

    model_config = ConfigDict(extra="forbid")

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
    voice: str = Field(default="", description="Voice override, in the backend's vocabulary")
    tts_backend: str = Field(
        default="edge",
        description=(
            "Engine that synthesizes this collection's digest audio: "
            "'edge', 'omni_voice' (the tts-api gateway), 'qwen3_tts' or "
            "'zonos2'. Empty inherits TTS_DEFAULT_BACKEND."
        ),
    )
    prompt_template: str = Field(
        default=DEFAULT_PROMPT_TEMPLATE,
        description="Prompt template name from DIGEST_TEMPLATE_ADDITIONS",
    )
    custom_prompt: str = Field(
        default="",
        description="Optional full custom system prompt overriding prompt_template",
    )
    chat_id: str | None = Field(
        default=None,
        description="Telegram chat id override; global TELEGRAM_CHAT_ID if empty",
    )
    email_recipients: list[str] = Field(
        default_factory=list,
        description="Email recipients for HTML email delivery (empty = off)",
    )
    whatsapp_recipients: list[str] = Field(
        default_factory=list,
        description=(
            "WhatsApp recipients (E.164) for audio/text delivery; empty disables WhatsApp"
        ),
    )
    whatsapp_group_id: str | None = Field(
        default=None,
        description="WhatsApp group id delivered as <id>@g.us, exempt from the allowlist",
    )
    delivery_channels: list[str] = Field(
        default_factory=lambda: ["telegram"],
        description=(
            "Delivery channels ('telegram', 'email', 'whatsapp'); empty defaults to telegram"
        ),
    )
    logic_delivery: LogicDeliveryConfig | None = Field(
        default=None,
        description=(
            "Optional admin destination for the LLM news-selection logic; when"
            " absent the pipeline does not generate any logic text"
        ),
    )
    narration_enabled: bool = Field(
        default=False, description="Generate narration audio for this collection"
    )
    narration_spec: NarrationSpec | None = Field(
        default=None, description="Narration spec (cast, music, defaults)"
    )
    narration_strategy: str = Field(
        default="turns", description="Render strategy: 'turns' or 'conversations'"
    )
    narration_deliver_audio: bool = Field(
        default=True, description="Deliver the narration audio to configured channels"
    )
    main_story_count: int = Field(
        default=5, ge=1, le=20, description="Number of main stories to include"
    )
    headline_count: int = Field(
        default=5, ge=1, le=40, description="Number of quick headlines to include"
    )
    # --- Phase 7.13: source reliability and LLM verification ---------------
    source_reliability: dict[str, float] = Field(
        default_factory=dict,
        description="Feed URL to reliability in [0, 1] (inline feed_urls tables)",
    )
    source_hosts: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Feed URL to extra article hosts it publishes under (aggregators)",
    )
    llm_verify: bool = Field(
        default=False,
        description="Verify the generated digest against its sources before delivering",
    )
    llm_verify_threshold: float | None = Field(
        default=None,
        description="Faithfulness threshold for this collection; empty = global setting",
    )
    llm_verify_backend: str | None = Field(
        default=None,
        description="Judge backend for this collection: 'local', 'modal' or 'api'",
    )
    audio_verify: bool = Field(
        default=False,
        description="Withhold the delivered audio when its WER exceeds the threshold",
    )
    audio_verify_threshold: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="WER ceiling for this collection; empty = global setting",
    )
    enabled: bool = Field(default=True, description="Pause a collection without deleting it")

    def to_config(self) -> CollectionConfig:
        """Validate and convert the payload into a CollectionConfig.

        Returns:
            A validated CollectionConfig built from this payload.
        """
        return CollectionConfig.model_validate(self.model_dump())

    @model_validator(mode="after")
    def _voice_matches_the_backend(self) -> CollectionIn:
        """Reject a digest voice the chosen engine cannot synthesize.

        Returns:
            The validated payload.

        Raises:
            ValueError: If ``voice`` belongs to another engine's vocabulary
                (Phase 9.2 guard; unknown ids pass so legacy data keeps
                loading).
        """
        from src.api.services.voice_engine import VoiceEngineError, assert_voice_engine

        try:
            assert_voice_engine(self.voice, self.tts_backend, field="voice")
        except VoiceEngineError as exc:
            raise ValueError(str(exc)) from exc
        return self


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
    ref_text: str | None = Field(
        default=None,
        description="Transcription of the reference audio (optional, improves cloning quality)",
    )


class VoiceCloneOut(BaseModel):
    """Response carrying the result of a voice clone request."""

    status: str = Field(default="ok", description="Execution status: ok or error")
    voice: dict[str, Any] = Field(..., description="The OmniVoice cloning response")


class RunOut(BaseModel):
    """Response carrying a single digest pipeline run."""

    status: str = Field(default="ok", description="Execution status: ok or error")
    run: dict[str, Any] = Field(..., description="Run row from digest_runs")


class AudioJobOut(BaseModel):
    """Response carrying a run's TTS generation status."""

    status: str = Field(default="ok", description="Execution status: ok or error")
    job: dict[str, Any] = Field(..., description="Audio job row from audio_jobs")


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
