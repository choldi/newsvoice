"""Pydantic models for text-to-speech generation."""

from pydantic import BaseModel, Field

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
