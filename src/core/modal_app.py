"""Modal app for narration TTS (Phase 7.8).

Runs on Modal with a GPU. The Qwen3-TTS base model is loaded once per
container (``@modal.enter``) and reused across every synthesis call, so a
whole episode costs one model load instead of one per turn.

This module is imported lazily by :mod:`src.core.modal_client` and is never
imported by the API or the local worker, so the ``modal`` package and the
Qwen3-TTS runtime stay out of the local image.
"""

from __future__ import annotations

import json
from pathlib import Path

import modal

app = modal.App("news-engine")

_VOLUME_PATH = "/voices"
_MANIFEST = "manifest.json"
_REFS_DIR = "refs"


def _ref_text(voice_ref: str) -> str:
    """Read the transcript that produced a reference sample.

    Qwen3-TTS clones in in-context-learning mode, which needs the exact text
    that generated the reference. The manifest is the source of truth for it,
    so a profile edit cannot silently desynchronise the transcript.

    Args:
        voice_ref: Reference voice name.

    Returns:
        The reference transcript.

    Raises:
        FileNotFoundError: When the manifest or the entry is missing.
    """
    manifest_path = Path(_VOLUME_PATH) / _MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = manifest[voice_ref]
    return entry["ref_text"]


@app.cls(
    gpu=modal.gpu.L4(),
    timeout=3600,
    volumes={_VOLUME_PATH: modal.Volume.from_name("news-engine-voices", create_if_missing=True)},
)
class QwenTTS:
    """Qwen3-TTS synthesis, loaded once per container."""

    @modal.enter()
    def load(self) -> None:
        """Load the base model once per container."""
        import torch
        from qwen_tts import Qwen3TTSModel

        self._model = Qwen3TTSModel.from_pretrained(
            "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
            device_map="cuda:0",
            dtype=torch.bfloat16,
            attn_implementation="flash_attention_2",
        )

    @modal.method()
    def generate_tts(
        self,
        *,
        text: str,
        voice_ref: str,
        language: str,
        speed: float = 1.0,
        emotion: str | None = None,
    ) -> bytes:
        """Synthesize one turn in the reference voice.

        Args:
            text: Speech text for the turn.
            voice_ref: Reference voice name; resolves to a WAV in the volume.
            language: ISO 639-1 language code.
            speed: Speaking rate in [0.5, 2.0]. Unused by Qwen3-TTS Base.
            emotion: Unused by Qwen3-TTS Base; accepted for ZONOS2 parity.

        Returns:
            WAV bytes at the model's native 24 kHz mono.
        """
        import io

        import soundfile as sf

        ref_path = f"{_VOLUME_PATH}/{_REFS_DIR}/{voice_ref}.wav"
        wavs, sample_rate = self._model.generate_voice_clone(
            text=text,
            language=language,
            ref_audio=ref_path,
            ref_text=_ref_text(voice_ref),
        )
        buffer = io.BytesIO()
        sf.write(buffer, wavs[0], sample_rate, format="WAV")
        return buffer.getvalue()


@app.cls(
    gpu=modal.gpu.L4(),
    timeout=1800,
)
class VoiceDesignModel:
    """Qwen3-TTS VoiceDesign, loaded once per container."""

    @modal.enter()
    def load(self) -> None:
        """Load the VoiceDesign model once per container."""
        import torch
        from qwen_tts import Qwen3TTSModel

        self._model = Qwen3TTSModel.from_pretrained(
            "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
            device_map="cuda:0",
            dtype=torch.bfloat16,
            attn_implementation="flash_attention_2",
        )

    @modal.method()
    def design(self, *, text: str, instruct: str, language: str) -> tuple[bytes, int]:
        """Generate a reference sample from a persona description.

        Args:
            text: The text to speak; becomes the reference transcript.
            instruct: Natural-language persona description.
            language: ISO 639-1 language code.

        Returns:
            Tuple of ``(wav_bytes, sample_rate)``.
        """
        import io

        import soundfile as sf

        wavs, sample_rate = self._model.generate_voice_design(
            text=text,
            language=language,
            instruct=instruct,
        )
        buffer = io.BytesIO()
        sf.write(buffer, wavs[0], sample_rate, format="WAV")
        return buffer.getvalue(), sample_rate


@app.function(
    gpu=modal.gpu.L4(),
    timeout=600,
    volumes={_VOLUME_PATH: modal.Volume.from_name("news-engine-voices", create_if_missing=True)},
)
def upload_voice(name: str, wav: bytes) -> None:
    """Store a reference voice in the Modal Volume.

    Args:
        name: Reference voice name.
        wav: Reference WAV bytes.
    """
    target = Path(_VOLUME_PATH) / _REFS_DIR / f"{name}.wav"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(wav)
