"""Mock TTS engine that generates sine tones (for testing)."""

from __future__ import annotations

import io
import math
import struct
import wave

from tts_api.core.engine_base import EngineCapabilities, TtsEngineBase


class MockEngine(TtsEngineBase):
    """Generates sine tones as WAV bytes. No model, no GPU, no dependencies."""

    @property
    def name(self) -> str:
        return "mock"

    @property
    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            stock_voices=True,
            voice_cloning=False,
            instruct_voice_design=False,
            emotions=False,
            streaming=False,
            supported_languages=["es", "en", "fr", "de", "it", "pt", "zh", "ja", "ko", "ca"],
        )

    async def initialize(self) -> None:
        pass

    async def synthesize(
        self,
        *,
        text: str,
        voice_id: str,
        language: str,
        speed: float = 1.0,
        emotion: str | None = None,
    ) -> bytes:
        return self._generate_tone(
            duration_sec=max(0.5, len(text) * 0.08),
            frequency=440.0,
        )

    async def synthesize_clone(
        self,
        *,
        text: str,
        reference_audio_path: str,
        language: str,
        speed: float = 1.0,
        ref_text: str | None = None,
    ) -> bytes:
        return self._generate_tone(
            duration_sec=max(0.5, len(text) * 0.08),
            frequency=880.0,
        )

    async def synthesize_instruct(
        self,
        *,
        text: str,
        instruct: str,
        language: str,
        speed: float = 1.0,
        emotion: str | None = None,
    ) -> bytes:
        return self._generate_tone(
            duration_sec=max(0.5, len(text) * 0.08),
            frequency=440.0,
        )

    async def list_stock_voices(self, language: str | None = None) -> list[dict]:
        voices = [
            {
                "voice_id": "es-mx-male",
                "language": "es",
                "gender": "male",
                "name": "Spanish MX Male",
            },
            {
                "voice_id": "es-mx-female",
                "language": "es",
                "gender": "female",
                "name": "Spanish MX Female",
            },
            {
                "voice_id": "es-es-male",
                "language": "es",
                "gender": "male",
                "name": "Spanish Spain Male",
            },
            {
                "voice_id": "es-es-female",
                "language": "es",
                "gender": "female",
                "name": "Spanish Spain Female",
            },
            {"voice_id": "ca-male", "language": "ca", "gender": "male", "name": "Catalan Male"},
            {
                "voice_id": "ca-female",
                "language": "ca",
                "gender": "female",
                "name": "Catalan Female",
            },
            {
                "voice_id": "en-us-male",
                "language": "en",
                "gender": "male",
                "name": "English US Male",
            },
            {
                "voice_id": "en-us-female",
                "language": "en",
                "gender": "female",
                "name": "English US Female",
            },
            {
                "voice_id": "en-gb-male",
                "language": "en",
                "gender": "male",
                "name": "English UK Male",
            },
            {
                "voice_id": "en-gb-female",
                "language": "en",
                "gender": "female",
                "name": "English UK Female",
            },
            {"voice_id": "fr-fr-male", "language": "fr", "gender": "male", "name": "French Male"},
            {
                "voice_id": "fr-fr-female",
                "language": "fr",
                "gender": "female",
                "name": "French Female",
            },
        ]
        if language:
            voices = [v for v in voices if v["language"] == language]
        return voices

    async def health_check(self) -> dict:
        return {
            "model_loaded": True,
            "gpu_available": False,
            "device": "cpu",
            "stock_voices_count": len(await self.list_stock_voices()),
            "vram_free_mb": 0,
            "mode": "MOCK",
            "engine": self.name,
        }

    def _generate_tone(
        self,
        duration_sec: float = 1.0,
        sample_rate: int = 22050,
        frequency: float = 440.0,
        amplitude: float = 0.3,
    ) -> bytes:
        num_samples = int(duration_sec * sample_rate)
        max_amplitude = 32767
        samples = []
        for i in range(num_samples):
            t = i / sample_rate
            value = amplitude * math.sin(2 * math.pi * frequency * t)
            sample = int(max_amplitude * value)
            samples.append(struct.pack("<h", sample))
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(b"".join(samples))
        return buffer.getvalue()
