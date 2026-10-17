"""Service for voice operations (cloning + designed)."""

from __future__ import annotations

import contextlib
import shutil
import uuid
from pathlib import Path

from loguru import logger

from tts_api.core.audio import AudioValidator
from tts_api.core.embedding_cache import EmbeddingCache, get_embedding_cache
from tts_api.core.exceptions import (
    FeatureNotSupportedError,
    InvalidReferenceAudioError,
    UnsupportedLanguageError,
    VoiceNotFoundError,
)
from tts_api.core.omnivoice_engine import _validate_instruct
from tts_api.repositories.voice_repository import VoiceRepository
from tts_api.settings import get_settings

# Engines that support voice cloning from reference audio
CLONE_CAPABLE_ENGINES = {"omnivoice", "pocket_tts"}


class VoiceService:
    """Service for voice operations (cloning + designed)."""

    def __init__(
        self,
        repository: VoiceRepository | None = None,
        audio_validator: AudioValidator | None = None,
        embedding_cache: EmbeddingCache | None = None,
    ) -> None:
        self._settings = get_settings()
        self._repository = repository
        self._audio_validator = audio_validator
        self._embedding_cache = embedding_cache
        self._initialized = repository is not None

    @property
    def _active_engine(self) -> str:
        """Name of the active engine (handles routed)."""
        engine = self._settings.TTS_ENGINE
        if engine == "routed":
            return "pocket_tts"
        return engine

    async def initialize(self) -> None:
        """Initialize the service and its dependencies."""
        if self._repository is None:
            self._repository = VoiceRepository()
            await self._repository.initialize()

        if self._audio_validator is None:
            self._audio_validator = AudioValidator(engine_name=self._active_engine)

        if self._embedding_cache is None:
            self._embedding_cache = get_embedding_cache()

        self._initialized = True

    async def clone_voice(
        self,
        name: str,
        language: str,
        reference_audio_path: Path | str,
        ref_text: str | None = None,
        engine: str | list[str] | None = None,
    ) -> str:
        """Clone a voice from reference audio.

        Args:
            name: Unique name for the cloned voice
            language: Language code (ISO 639-1)
            reference_audio_path: Path to the reference audio file
            ref_text: Transcription of the reference audio (optional, improves cloning)
            engine: Engine(s) to associate. Accepts "pocket_tts",
                    "pocket_tts,omnivoice", or list. None = active engine.

        Returns:
            str: The UUID of the cloned voice

        Raises:
            ValueError: If voice name already exists
            UnsupportedLanguageError: If language is not supported
            InvalidReferenceAudioError: If reference audio is invalid
            FeatureNotSupportedError: If an engine doesn't support cloning
        """
        # Parse engine(s)
        if engine is None:
            engine_str = self._active_engine
        elif isinstance(engine, list):
            engine_str = ",".join(engine)
        else:
            engine_str = engine.strip()

        engine_list = [e.strip() for e in engine_str.split(",") if e.strip()]

        # Validate each engine supports cloning
        for e in engine_list:
            if e not in CLONE_CAPABLE_ENGINES:
                raise FeatureNotSupportedError("voice cloning", e)

        # Validate language
        if language not in self._settings.omnilang_list:
            raise UnsupportedLanguageError(language, self._settings.omnilang_list)

        # Pick sample rate: 24000 if pocket_tts included (max quality), else 22050
        validator_engine = "pocket_tts" if "pocket_tts" in engine_list else engine_list[0]

        audio_validator = AudioValidator(engine_name=validator_engine)
        validated_path, duration_sec = await audio_validator.validate_and_prepare(
            reference_audio_path, language
        )

        # Check max duration
        if duration_sec > self._settings.MAX_REFERENCE_DURATION_SEC:
            raise InvalidReferenceAudioError(
                f"Reference audio too long: {duration_sec:.1f}s > "
                f"{self._settings.MAX_REFERENCE_DURATION_SEC}s"
            )

        # Copy to permanent storage
        voices_dir = self._settings.VOICES_DIR
        voices_dir.mkdir(parents=True, exist_ok=True)

        voice_id = str(uuid.uuid4())
        dest_path = voices_dir / voice_id / "reference.wav"
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        shutil.copy2(validated_path, dest_path)

        metadata = {"original_path": str(reference_audio_path)}
        if ref_text:
            metadata["ref_text"] = ref_text

        # Store in repository with engine tag (comma-separated)
        engine_tag = ",".join(engine_list)
        voice_id = await self._repository.create(
            name=name,
            language=language,
            reference_path=str(dest_path),
            duration_sec=duration_sec,
            metadata=metadata,
            engine=engine_tag,
        )

        # Pre-compute and cache embedding
        try:
            self._embedding_cache.invalidate(dest_path)
        except Exception as e:
            logger.warning(f"Failed to precompute embedding for {voice_id}: {e}")

        logger.info(f"Voice cloned successfully: {name} ({voice_id}, engines={engine_tag})")
        return voice_id

    async def get_voice(self, voice_id: str) -> dict:
        """Get a cloned voice by ID. Validates engine compatibility."""
        voice = await self._repository.get_by_id(voice_id)
        self._validate_engine(voice)
        return voice

    async def list_voices(
        self,
        language: str | None = None,
        limit: int = 100,
        offset: int = 0,
        engine: str | None = None,
    ) -> list[dict]:
        """List cloned voices with optional filtering."""
        return await self._repository.list(
            language=language,
            limit=limit,
            offset=offset,
            engine=engine,
        )

    async def delete_voice(self, voice_id: str) -> bool:
        """Delete a cloned voice."""
        ref_path = None
        try:
            voice = await self._repository.get_by_id(voice_id)
            ref_path = Path(voice["reference_path"])
            if ref_path.exists():
                shutil.rmtree(ref_path.parent, ignore_errors=True)
        except VoiceNotFoundError:
            pass

        deleted = await self._repository.delete(voice_id)

        if deleted and ref_path is not None:
            with contextlib.suppress(Exception):
                self._embedding_cache.invalidate(ref_path)

        return deleted

    async def voice_exists(self, voice_id: str) -> bool:
        """Check if a voice exists."""
        return await self._repository.voice_exists(voice_id)

    def _validate_engine(self, voice: dict) -> None:
        """Validate that the voice's engine(s) include the active engine.

        The voice's engine column stores comma-separated engines
        (e.g. "pocket_tts,omnivoice").

        Raises:
            FeatureNotSupportedError: If active engine not in voice's engine list.
        """
        voice_engines_raw = voice.get("engine", "omnivoice")
        voice_engines = {e.strip() for e in voice_engines_raw.split(",") if e.strip()}
        active = self._active_engine

        # For routed engines, accept voices tagged with any clone-capable engine
        if self._settings.TTS_ENGINE == "routed" and voice_engines & CLONE_CAPABLE_ENGINES:
            return

        # Direct match: active engine is in the voice's engine list
        if active in voice_engines:
            return

        raise FeatureNotSupportedError(
            f"cloned voice (engines={voice_engines_raw})",
            active,
        )

    # --- Designed voices (instruct-based presets) ---

    async def create_designed_voice(
        self,
        name: str,
        instruct: str,
        language: str,
    ) -> str:
        """Create a designed voice from an instruct string."""
        if language not in self._settings.omnilang_list:
            raise UnsupportedLanguageError(language, self._settings.omnilang_list)

        _validate_instruct(instruct)

        voice_id = await self._repository.create_designed_voice(
            name=name,
            instruct=instruct,
            language=language,
        )
        logger.info(f"Designed voice created: {name} ({voice_id})")
        return voice_id

    async def get_designed_voice(self, voice_id: str) -> dict:
        """Get a designed voice by ID."""
        return await self._repository.get_designed_voice(voice_id)

    async def list_designed_voices(
        self,
        language: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """List designed voices with optional filtering."""
        return await self._repository.list_designed_voices(
            language=language,
            limit=limit,
            offset=offset,
        )

    async def delete_designed_voice(self, voice_id: str) -> bool:
        """Delete a designed voice."""
        return await self._repository.delete_designed_voice(voice_id)
