"""Abstract base class for TTS engines and capability system."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class EngineCapabilities:
    """Feature flags declaring what an engine supports."""

    stock_voices: bool = True
    voice_cloning: bool = False
    instruct_voice_design: bool = False
    emotions: bool = False
    streaming: bool = False
    supported_languages: list[str] = field(default_factory=list)


class TtsEngineBase(ABC):
    """Abstract interface that all TTS engine backends must implement."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable engine name (e.g. 'pocket_tts', 'edgetts')."""

    @property
    @abstractmethod
    def capabilities(self) -> EngineCapabilities:
        """Declare what this engine supports."""

    @abstractmethod
    async def initialize(self) -> None:
        """Load model / verify connectivity."""

    @abstractmethod
    async def synthesize(
        self,
        *,
        text: str,
        voice_id: str,
        language: str,
        speed: float = 1.0,
        emotion: str | None = None,
    ) -> bytes:
        """Synthesize text with a stock voice. Returns WAV bytes."""

    @abstractmethod
    async def synthesize_clone(
        self,
        *,
        text: str,
        reference_audio_path: str,
        language: str,
        speed: float = 1.0,
        ref_text: str | None = None,
    ) -> bytes:
        """Synthesize text cloning a voice from reference audio. Returns WAV bytes."""

    @abstractmethod
    async def synthesize_instruct(
        self,
        *,
        text: str,
        instruct: str,
        language: str,
        speed: float = 1.0,
        emotion: str | None = None,
    ) -> bytes:
        """Synthesize text with a voice-design instruct. Returns WAV bytes."""

    @abstractmethod
    async def list_stock_voices(self, language: str | None = None) -> list[dict]:
        """Return available stock voices, optionally filtered by language."""

    @abstractmethod
    async def health_check(self) -> dict:
        """Return engine health status dict."""

    async def warmup(self) -> None:
        """Optional warmup (no-op by default)."""

    async def close(self) -> None:
        """Release resources (no-op by default)."""

    async def list_available_models(self) -> list[str]:
        """Optional: List available models for this engine.
        Returns empty list if not applicable or not implemented."""
        return []

    async def list_available_features(self) -> dict:
        """Optional: Return detailed feature information.
        Default implementation returns capabilities as dict."""
        return self.capabilities.__dict__

    async def change_model(self, model_name: str) -> bool:
        """Optional: Attempt to change model at runtime.
        Returns False if not supported or fails."""
        return False
