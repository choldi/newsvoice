"""Servicio de síntesis de texto a voz (TTS) — engine-agnostic."""

from __future__ import annotations

from tts_api.core.engine_client import (
    AudioResult,
    OmniVoiceEngineClient,
    get_shared_engine_client,
)
from tts_api.core.exceptions import (
    UnsupportedEmotionError,
    UnsupportedLanguageError,
    VoiceNotFoundError,
)
from tts_api.services.voice_service import VoiceService
from tts_api.settings import get_settings


class TtsService:
    """Servicio de síntesis de voz (funciona con cualquier engine)."""

    def __init__(
        self,
        engine_client: OmniVoiceEngineClient | None = None,
        voice_service: VoiceService | None = None,
    ) -> None:
        self._settings = get_settings()
        self._engine_client = engine_client
        self._voice_service = voice_service

    def _validate_language(self, language: str) -> None:
        """Rechaza idiomas fuera de la lista soportada antes de llamar al engine.

        Args:
            language: Idioma solicitado (ISO 639-1).

        Raises:
            UnsupportedLanguageError: Si el idioma no está soportado.
        """
        supported = list(self._settings.omnilang_list)
        if language not in supported:
            raise UnsupportedLanguageError(language, supported)

    def _validate_emotion(self, emotion: str | None) -> None:
        """Rechaza emociones fuera de la lista soportada por OmniVoice.

        Args:
            emotion: Emoción solicitada o ``None``.

        Raises:
            UnsupportedEmotionError: Si la emoción no está soportada.
        """
        if emotion is None:
            return
        from tts_api.core.omnivoice_engine import SUPPORTED_EMOTIONS

        if emotion not in SUPPORTED_EMOTIONS:
            raise UnsupportedEmotionError(emotion, list(SUPPORTED_EMOTIONS))

    async def _get_engine_client(self) -> OmniVoiceEngineClient:
        if self._engine_client is None:
            # Cliente compartido por el proceso: construir uno propio aquí
            # recargaría los pesos del modelo en cada petición.
            self._engine_client = get_shared_engine_client()
            await self._engine_client.start()
        return self._engine_client

    async def _get_voice_service(self) -> VoiceService:
        if self._voice_service is None:
            self._voice_service = VoiceService()
            await self._voice_service.initialize()
        elif not getattr(self._voice_service, "_initialized", False):
            await self._voice_service.initialize()
        return self._voice_service

    async def synthesize_stock(
        self,
        *,
        text: str,
        voice_id: str,
        language: str,
        speed: float = 1.0,
        emotion: str | None = None,
    ) -> AudioResult:
        """Sintetiza texto con voz stock, designed o clonada.

        Lookup order:
          1. Cloned voice (reference audio)
          2. Designed voice (instruct preset from DB)
          3. Stock voice (hardcoded mapping)
        """
        self._validate_language(language)
        self._validate_emotion(emotion)
        # 1. Try cloned voice
        try:
            voice_service = await self._get_voice_service()
            await voice_service.get_voice(voice_id)
            return await self.synthesize_clone(
                text=text,
                voice_id=voice_id,
                language=language,
                speed=speed,
            )
        except VoiceNotFoundError:
            pass
        except Exception as e:
            import logging

            logging.getLogger(__name__).warning("Cloned voice lookup failed: %s", e)

        # 2. Try designed voice (instruct preset from DB)
        try:
            voice_service = await self._get_voice_service()
            designed = await voice_service.get_designed_voice(voice_id)
            engine = await self._get_engine_client()
            return await engine.synthesize_instruct(
                text=text,
                instruct=designed["instruct"],
                speed=speed,
                emotion=emotion,
                language=language,
            )
        except VoiceNotFoundError:
            pass
        except Exception as e:
            import logging

            logging.getLogger(__name__).warning("Designed voice synthesis failed: %s", e)

        # 3. Fall back to stock voices
        engine = await self._get_engine_client()

        stock_voices = await engine.list_stock_voices(language)
        if not any(v.voice_id == voice_id for v in stock_voices):
            valid_ids = [v.voice_id for v in stock_voices]
            err = VoiceNotFoundError(voice_id, "stock")
            err.valid_voice_ids = valid_ids
            raise err

        return await engine.synthesize_stock(
            text=text,
            voice_id=voice_id,
            speed=speed,
            emotion=emotion,
            language=language,
        )

    async def synthesize_instruct(
        self,
        *,
        text: str,
        instruct: str,
        language: str,
        speed: float = 1.0,
        emotion: str | None = None,
    ) -> AudioResult:
        """Sintetiza texto con instruct personalizado (voice design libre)."""
        self._validate_language(language)
        self._validate_emotion(emotion)
        engine = await self._get_engine_client()
        return await engine.synthesize_instruct(
            text=text,
            instruct=instruct,
            speed=speed,
            emotion=emotion,
            language=language,
        )

    async def synthesize_clone(
        self,
        *,
        text: str,
        voice_id: str,
        language: str,
        speed: float = 1.0,
    ) -> AudioResult:
        """Sintetiza texto con voz clonada."""
        voice_service = await self._get_voice_service()
        voice_data = await voice_service.get_voice(voice_id)
        reference_audio_path = voice_data["reference_path"]

        # Use the voice's own language from the DB, with the API parameter as fallback.
        voice_language = voice_data.get("language") or language

        # Retrieve ref_text (pre-computed transcription) from metadata
        ref_text = voice_data.get("metadata", {}).get("ref_text")

        engine = await self._get_engine_client()

        return await engine.synthesize_clone(
            text=text,
            reference_audio_path=reference_audio_path,
            ref_text=ref_text,
            speed=speed,
            language=voice_language,
        )

    async def close(self) -> None:
        """Suelta la referencia al cliente sin descargar el engine.

        El cliente de engine es compartido por el proceso: detenerlo aquí
        descargaría los pesos del modelo y obligaría a recargarlos en la
        siguiente petición. Su ciclo de vida real lo lleva el lifespan de la
        API.
        """
        self._engine_client = None
