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
    timeout=3600,
    volumes={_VOLUME_PATH: modal.Volume.from_name("news-engine-voices", create_if_missing=True)},
)
class Zonos2TTS:
    """ZONOS2 synthesis with native speed and emotion control.

    ZONOS2 is the gated backend: it adds what Qwen3-TTS Base cannot do —
    first-class speaking-rate control and timbre-preserving emotion directions.
    It outputs 44.1 kHz via DAC, which the canonical resample reduces to 24 kHz.
    """

    @modal.enter()
    def load(self) -> None:
        """Load ZONOS2 once per container."""
        from zonos2.tts import TTSLLM

        self._tts = TTSLLM(model_path="Zyphra/ZONOS2")

    @modal.method()
    def generate_tts_zonos2(
        self,
        *,
        text: str,
        voice_ref: str,
        language: str,
        speed: float = 1.0,
        emotion: str | None = None,
    ) -> bytes:
        """Synthesize one turn in the reference voice with ZONOS2.

        Args:
            text: Speech text for the turn.
            voice_ref: Reference voice name; resolves to a WAV in the volume.
            language: ISO 639-1 language code.
            speed: Speaking rate multiplier; native ZONOS2 conditioning.
            emotion: Optional emotion tag (happy, sad, angry, surprised).

        Returns:
            WAV bytes at 44.1 kHz.
        """
        import io

        import numpy as np
        import soundfile as sf
        from zonos2.message import TTSSamplingParams

        ref_path = f"{_VOLUME_PATH}/{_REFS_DIR}/{voice_ref}.wav"
        emb = self._tts.embed_speaker_file(ref_path)
        emotion_sliders = {emotion: 1.0} if emotion else None
        result = self._tts.generate_one(
            text,
            TTSSamplingParams(seed=42),
            speaker_embedding=emb,
            language=language,
            speed=speed,
            emotion_enabled=emotion is not None,
            emotion_sliders=emotion_sliders,
            accurate_mode=False,
            emotion_cfg_scale=1.5 if emotion else 1.0,
        )
        audio = result["audio"]
        if hasattr(audio, "detach"):
            audio = audio.detach().cpu().numpy()
        buffer = io.BytesIO()
        sf.write(buffer, np.asarray(audio), 44100, format="WAV")
        return buffer.getvalue()


@app.function(
    gpu=modal.gpu.L4(),
    timeout=1800,
    volumes={_VOLUME_PATH: modal.Volume.from_name("news-engine-voices", create_if_missing=True)},
)
def benchmark_zonos(voice_ref: str, text: str, language: str) -> dict:
    """Run the ZONOS2 gate benchmark for one reference voice.

    Measures the four criteria that decide whether ZONOS2 is activated:
    speaker similarity, emotion artifacts, speed fidelity and bandwidth.

    Args:
        voice_ref: Reference voice name; resolves to a WAV in the volume.
        text: Text to synthesize for the measurements.
        language: ISO 639-1 language code.

    Returns:
        Dict with the per-criterion results and an overall ``pass``.
    """
    import tempfile

    import numpy as np
    import soundfile as sf
    from zonos2.message import TTSSamplingParams
    from zonos2.tts import TTSLLM

    tts = TTSLLM(model_path="Zyphra/ZONOS2")
    ref_path = f"{_VOLUME_PATH}/{_REFS_DIR}/{voice_ref}.wav"
    ref_emb = tts.embed_speaker_file(ref_path)

    def _to_numpy(audio) -> np.ndarray:
        if hasattr(audio, "detach"):
            return audio.detach().cpu().numpy()
        return np.asarray(audio)

    def _embed(audio: np.ndarray, sample_rate: int):
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            sf.write(tmp.name, audio, sample_rate, format="WAV")
            return tts.embed_speaker_file(tmp.name)

    criteria: dict[str, dict] = {}

    result = tts.generate_one(
        text, TTSSamplingParams(seed=42), speaker_embedding=ref_emb, language=language
    )
    gen = _to_numpy(result["audio"])
    gen_emb = _embed(gen, 44100)
    similarity = float(
        np.dot(ref_emb, gen_emb) / (np.linalg.norm(ref_emb) * np.linalg.norm(gen_emb))
    )
    criteria["speaker_similarity"] = {
        "value": round(similarity, 4),
        "threshold": 0.75,
        "pass": similarity >= 0.75,
    }

    result_emo = tts.generate_one(
        text,
        TTSSamplingParams(seed=42),
        speaker_embedding=ref_emb,
        language=language,
        emotion_enabled=True,
        emotion_sliders={"happy": 1.0},
        accurate_mode=False,
        emotion_cfg_scale=1.5,
    )
    emo = _to_numpy(result_emo["audio"])
    peak = float(np.max(np.abs(emo))) if emo.size else 0.0
    criteria["emotion_artifacts"] = {
        "peak": round(peak, 4),
        "pass": peak <= 0.99,
    }

    slow = _to_numpy(
        tts.generate_one(
            text,
            TTSSamplingParams(seed=42),
            speaker_embedding=ref_emb,
            language=language,
            speed=1.0,
        )["audio"]
    )
    fast = _to_numpy(
        tts.generate_one(
            text,
            TTSSamplingParams(seed=42),
            speaker_embedding=ref_emb,
            language=language,
            speed=1.2,
        )["audio"]
    )
    dur_slow = len(slow) / 44100
    dur_fast = len(fast) / 44100
    ratio = dur_slow / dur_fast if dur_fast > 0 else 0.0
    criteria["speed_fidelity"] = {
        "ratio": round(ratio, 4),
        "threshold": 1.1,
        "pass": ratio >= 1.1,
    }

    spectrum = np.abs(np.fft.rfft(gen))
    freqs = np.fft.rfftfreq(len(gen), 1 / 44100)
    total = float(np.sum(spectrum))
    high_ratio = float(np.sum(spectrum[freqs > 12000])) / total if total > 0 else 0.0
    criteria["bandwidth"] = {
        "high_ratio": round(high_ratio, 6),
        "pass": high_ratio < 0.05,
    }

    return {
        "voice_ref": voice_ref,
        "criteria": criteria,
        "pass": all(c["pass"] for c in criteria.values()),
    }


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
