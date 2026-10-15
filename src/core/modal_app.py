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


def _dnsmos_ovrl(mos: object, wav: object, sample_rate: int) -> float | None:
    """Score one clip with the no-reference DNSMOS P.835 overall MOS.

    The clip is resampled to the 16 kHz DNSMOS expects with a plain linear
    interpolation: it is only ever compared against sibling takes of the same
    speaker and text, so any dependency-free resampler preserves the ordering.

    Args:
        mos: The loaded ``torch_dnsmos`` scorer.
        wav: Audio as a float array of samples.
        sample_rate: Sample rate of the clip.

    Returns:
        The overall MOS, or None when the clip could not be scored.
    """
    import numpy as np
    import torch

    try:
        waveform = torch.from_numpy(np.asarray(wav, dtype=np.float32)).flatten()
        if waveform.numel() == 0:
            return None
        if sample_rate != 16000:
            length = int(round(waveform.shape[-1] * 16000 / sample_rate))
            if length < 1:
                return None
            waveform = torch.nn.functional.interpolate(
                waveform[None, None], size=length, mode="linear", align_corners=False
            )[0, 0]
        prediction = mos.predict(waveform, sample_rate=16000)
        scores = prediction.get("scores") if isinstance(prediction, dict) else None
        if isinstance(scores, dict) and "OVRL" in scores:
            return float(scores["OVRL"])
        return None
    except Exception:
        return None


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


#: VoxCPM2 (Apache-2.0, 2B) needs its own image: `voxcpm` pulls the MiniCPM-4
#: backbone and its sampling kernels. `torch-dnsmos` adds the no-reference MOS
#: used to keep the best of several takes without a second container.
_VOXCPM_IMAGE = modal.Image.debian_slim(python_version="3.12").pip_install(
    "torch", "voxcpm", "soundfile", "numpy", "torch-dnsmos"
)


@app.cls(
    image=_VOXCPM_IMAGE,
    gpu=modal.gpu.L4(),
    timeout=3600,
    volumes={_VOLUME_PATH: modal.Volume.from_name("news-engine-voices", create_if_missing=True)},
)
class VoxCPM2TTS:
    """VoxCPM2 synthesis, loaded once per container.

    VoxCPM2 clones in audio-continuation mode: it takes the reference clip and
    its exact transcript, the same pair Qwen3-TTS and ZONOS2 read from the
    volume. It emits native 48 kHz mono.
    """

    @modal.enter()
    def load(self) -> None:
        """Load VoxCPM2 and the DNSMOS scorer once per container."""
        import torch_dnsmos
        from voxcpm import VoxCPM

        self._model = VoxCPM.from_pretrained("openbmb/VoxCPM2", load_denoiser=False)
        # P.835 SIG/BAK/OVRL; OVRL is the single figure used to rank takes.
        self._mos = torch_dnsmos.load("sig_bak_ovr")

    @modal.method()
    def generate_tts_voxcpm2(
        self,
        *,
        text: str,
        voice_ref: str,
        language: str,
        speed: float = 1.0,
        emotion: str | None = None,
        cfg_value: float = 2.0,
        inference_timesteps: int = 10,
        num_candidates: int = 1,
    ) -> bytes:
        """Synthesize one turn in the reference voice with VoxCPM2.

        Args:
            text: Speech text for the turn.
            voice_ref: Reference voice name; resolves to a WAV in the volume.
            language: ISO 639-1 language code; VoxCPM2 infers it from the text.
            speed: Unused by VoxCPM2; the caller post-processes with atempo.
            emotion: Unused by VoxCPM2; accepted for backend parity.
            cfg_value: Classifier-free-guidance scale.
            inference_timesteps: Diffusion steps; higher is slower and crisper.
            num_candidates: Takes to synthesize before DNSMOS picks the best.

        Returns:
            WAV bytes at VoxCPM2's native 48 kHz mono. When several takes are
            requested, the one with the highest no-reference DNSMOS overall
            score is returned.
        """
        import io

        import soundfile as sf

        ref_path = f"{_VOLUME_PATH}/{_REFS_DIR}/{voice_ref}.wav"
        sample_rate = self._model.tts_model.sample_rate
        takes = [
            self._model.generate(
                text=text,
                prompt_wav_path=ref_path,
                prompt_text=_ref_text(voice_ref),
                reference_wav_path=ref_path,
                cfg_value=cfg_value,
                inference_timesteps=inference_timesteps,
            )
            for _ in range(max(1, num_candidates))
        ]
        wav = takes[0]
        if len(takes) > 1:

            def _rank(index: int) -> tuple[bool, float]:
                """Prefer a scored take; an unscoreable one ranks last."""
                score = _dnsmos_ovrl(self._mos, takes[index], sample_rate)
                return (score is not None, score if score is not None else 0.0)

            wav = takes[max(range(len(takes)), key=_rank)]

        buffer = io.BytesIO()
        sf.write(buffer, wav, sample_rate, format="WAV")
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
    timeout=1800,
    volumes={_VOLUME_PATH: modal.Volume.from_name("news-engine-voices", create_if_missing=True)},
)
def benchmark_voxcpm2(voice_ref: str, text: str, language: str, num_candidates: int = 3) -> dict:
    """Run the VoxCPM2 gate benchmark for one reference voice.

    Measures the criteria that decide whether VoxCPM2 is activated over the
    current renderer: native 48 kHz, no clipping, a decent no-reference MOS
    and genuine wideband content. It also reports the best-of-N MOS gain.

    Args:
        voice_ref: Reference voice name; resolves to a WAV in the volume.
        text: Text to synthesize for the measurements.
        language: ISO 639-1 language code; VoxCPM2 infers it from the text.
        num_candidates: Takes synthesized before DNSMOS picks the best.

    Returns:
        Dict with the per-criterion results and an overall ``pass``.
    """
    import numpy as np
    import torch_dnsmos
    from voxcpm import VoxCPM

    model = VoxCPM.from_pretrained("openbmb/VoxCPM2", load_denoiser=False)
    mos = torch_dnsmos.load("sig_bak_ovr")
    ref_path = f"{_VOLUME_PATH}/{_REFS_DIR}/{voice_ref}.wav"
    sample_rate = model.tts_model.sample_rate

    takes = [
        model.generate(
            text=text,
            prompt_wav_path=ref_path,
            prompt_text=_ref_text(voice_ref),
            reference_wav_path=ref_path,
        )
        for _ in range(max(1, num_candidates))
    ]
    ovrl = [_dnsmos_ovrl(mos, take, sample_rate) for take in takes]
    first_ovrl = ovrl[0]
    scored = [score for score in ovrl if score is not None]
    best_ovrl = max(scored) if scored else None
    best = max(
        range(len(takes)),
        key=lambda i: (ovrl[i] is not None, ovrl[i] if ovrl[i] is not None else 0.0),
    )
    wav = np.asarray(takes[best], dtype=np.float32).flatten()

    peak = float(np.max(np.abs(wav))) if wav.size else 0.0

    spectrum = np.abs(np.fft.rfft(wav))
    freqs = np.fft.rfftfreq(len(wav), 1 / sample_rate)
    total = float(np.sum(spectrum))
    wide_ratio = float(np.sum(spectrum[freqs > 16000])) / total if total > 0 else 0.0

    criteria = {
        "sample_rate": {
            "value": sample_rate,
            "expected": 48000,
            "pass": sample_rate == 48000,
        },
        "no_clipping": {
            "peak": round(peak, 4),
            "pass": peak <= 0.99,
        },
        "mos": {
            "value": round(best_ovrl, 4) if best_ovrl is not None else None,
            "threshold": 3.0,
            "pass": best_ovrl is not None and best_ovrl >= 3.0,
        },
        "wideband": {
            "high_ratio": round(wide_ratio, 6),
            "threshold": 0.02,
            "pass": wide_ratio >= 0.02,
        },
    }

    return {
        "voice_ref": voice_ref,
        "takes": {
            "count": len(takes),
            "ovrl": [round(score, 4) if score is not None else None for score in ovrl],
            "best_index": best,
            "best_gain": round(best_ovrl - first_ovrl, 4)
            if best_ovrl is not None and first_ovrl is not None
            else None,
        },
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


# --- Phase 7.13: LLM judge for digest verification ---------------------------
# A separate image from the TTS one: the judge needs transformers and an ungated
# instruct model, the TTS class needs qwen-tts. Both live in the same app, so
# `modal deploy src/core/modal_app.py` ships them together, but each function
# only pays for its own image.

_JUDGE_IMAGE = modal.Image.debian_slim(python_version="3.12").pip_install(
    "torch", "transformers>=4.51", "accelerate"
)


@app.function(
    image=_JUDGE_IMAGE,
    gpu=modal.gpu.L4(),
    timeout=900,
    # 30 GB of disk keeps the 8B weights cached across container reuse.
    block_network=False,
)
def judge(system_prompt: str, user_prompt: str, model: str, max_tokens: int = 2048) -> dict:
    """Run one verification judgement on Modal.

    Loads the model per call rather than per container: a digest needs a handful
    of short judgements, so a cached container is worth less than the simplicity.

    Args:
        system_prompt: Judge system prompt.
        user_prompt: Judge user prompt.
        model: HuggingFace model id.
        max_tokens: Response budget.

    Returns:
        Dict with the raw ``content`` the model produced.
    """
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(model)
    llm = AutoModelForCausalLM.from_pretrained(
        model,
        dtype=torch.bfloat16,
        device_map="auto",
    )
    chat = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]
    inputs = tokenizer.apply_chat_template(
        chat, add_generation_prompt=True, return_tensors="pt"
    ).to(llm.device)
    generated = llm.generate(
        inputs, max_new_tokens=max_tokens, do_sample=False, temperature=None, top_p=None
    )
    new_tokens = generated[0][inputs.shape[-1] :]
    return {"content": tokenizer.decode(new_tokens, skip_special_tokens=True)}
