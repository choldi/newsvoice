"""Pydantic models for text-to-speech generation."""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from src.api.models.narration import TTSBackend


class TTSIn(BaseModel):
    """Input payload to generate audio from markdown text."""

    markdown: str = Field(
        ...,
        min_length=1,
        # Hard ceiling on the request body; longer digests are still truncated to
        # TTS_MAX_CHARS by the service, so this only bounds abusive payloads.
        max_length=20_000,
        description="Markdown text to synthesize (truncated to TTS_MAX_CHARS)",
    )
    voice: str | None = Field(
        default=None,
        description=(
            "Voice in the chosen backend's own vocabulary: an Edge neural "
            "ShortName (e.g. 'es-MX-JorgeNeural') or legacy alias for edge, a "
            "tts-api stock id or clone UUID for omni_voice, a voice-catalog "
            "profile name for the Modal backends."
        ),
    )
    language: str | None = Field(
        default=None, description="ISO 639-1 language code (e.g. 'es'). Defaults to settings."
    )
    collection: str = Field(
        default="default",
        description="Collection name used to name the output file "
        "news_<collection>_<yymmdd>.<ext>.",
    )
    backend: TTSBackend | None = Field(
        default=None,
        description="Synthesis engine; empty falls back to TTS_DEFAULT_BACKEND.",
    )

    @model_validator(mode="after")
    def _voice_matches_the_backend(self) -> TTSIn:
        """Reject a voice the (explicit or default) engine cannot synthesize.

        Returns:
            The validated payload.

        Raises:
            ValueError: If the voice belongs to another engine's vocabulary
                (Phase 9.2 guard; unknown ids and an empty voice pass).
        """
        from src.api.services.voice_engine import VoiceEngineError, assert_voice_engine

        if self.backend is None:
            from src.core.config import get_settings

            backend = get_settings().tts_default_backend
        else:
            backend = str(self.backend)
        try:
            assert_voice_engine(self.voice or "", backend, field="voice")
        except VoiceEngineError as exc:
            raise ValueError(str(exc)) from exc
        return self


class TTSOut(BaseModel):
    """Response payload containing the audio file location and metadata."""

    status: str = Field(default="ok", description="Execution status: ok or error")
    audio_path: str = Field(..., description="Path to the generated audio file")
    duration_estimate: float = Field(..., description="Estimated audio duration in seconds")
    estimate_wall_seconds: float | None = Field(
        default=None,
        description="Estimated wall-clock seconds this synthesis needs (Phase 8.5)",
    )
    cached: bool = Field(..., description="Whether the audio was served from cache")
    voice: str = Field(
        ...,
        description=(
            "Voice as configured for this run. The id actually handed to the "
            "engine (which may differ: aliases are resolved per backend) is "
            "recorded in the cache sidecar."
        ),
    )
    backend: str = Field(default="", description="Engine that produced the audio")
    char_count: int = Field(..., description="Number of characters synthesized")
