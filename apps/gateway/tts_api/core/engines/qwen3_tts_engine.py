"""Qwen3-TTS engine — local clone / voice-design / custom voices.

The heavy runtime lives in a separate virtualenv (``QWEN3_TTS_PYTHON``) because
``qwen-tts`` pins ``transformers``/``accelerate`` versions that clash with the
gateway process. This engine therefore drives a long-lived :mod:`qwen3_tts_worker`
subprocess over a line protocol: the model is loaded once (on first use of each
kind) and reused across requests, so a whole episode does not pay a cold start
per segment.

The engine declares ``locations=["local"]``: it is the gateway's local
counterpart of the cloud (Modal) Qwen3-TTS backends used by clients such as
news-engine.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import tempfile
import threading
from pathlib import Path

from tts_api.core.engine_base import EngineCapabilities, TtsEngineBase
from tts_api.core.engines.qwen3_tts_worker import RESPONSE_PREFIX
from tts_api.core.exceptions import (
    EngineUnavailableError,
    FeatureNotSupportedError,
    UnsupportedLanguageError,
)
from tts_api.settings import get_settings

logger = logging.getLogger(__name__)

#: Seconds to wait for a single synthesis response from the worker.
_REQUEST_TIMEOUT_SEC = 600.0

#: ISO 639-1 -> Qwen3-TTS language name.
LANGUAGE_NAMES: dict[str, str] = {
    "en": "english",
    "es": "spanish",
    "fr": "french",
    "ca": "french",
    "pt": "portuguese",
    "de": "german",
    "it": "italian",
    "ja": "japanese",
    "ko": "korean",
    "ru": "russian",
    "zh": "chinese",
}


class Qwen3TTSEngine(TtsEngineBase):
    """Qwen3-TTS driven through an out-of-process worker."""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._request_id = 0

    @property
    def name(self) -> str:
        return "qwen3_tts"

    @property
    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            stock_voices=False,
            voice_cloning=True,
            instruct_voice_design=True,
            emotions=False,
            streaming=False,
            supported_languages=list(LANGUAGE_NAMES),
            locations=["local"],
        )

    # --- lifecycle ---------------------------------------------------------

    def _python_bin(self) -> Path:
        return self._settings.qwen3_tts_python

    async def initialize(self) -> None:
        """Verify the external runtime exists (models load lazily on first use).

        Missing runtime is non-fatal: the engine stays unavailable for
        synthesis but does not prevent the gateway (or a routed engine that
        includes it) from starting.
        """
        python_bin = self._python_bin()
        if not python_bin.exists():
            logger.warning(
                "Intérprete de qwen-tts no encontrado: %s (configura QWEN3_TTS_PYTHON); "
                "el engine qwen3_tts quedará no disponible",
                python_bin,
            )

    async def warmup(self) -> None:
        """Spawn the worker but do not load any model yet."""
        await asyncio.to_thread(self._ensure_worker)

    async def close(self) -> None:
        """Terminate the worker subprocess."""
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    # --- worker plumbing ---------------------------------------------------

    def _worker_args(self) -> list[str]:
        s = self._settings
        return [
            str(self._python_bin()),
            str(Path(__file__).with_name("qwen3_tts_worker.py")),
            "--device",
            s.QWEN3_TTS_DEVICE,
            "--dtype",
            s.QWEN3_TTS_DTYPE,
            "--attn",
            s.QWEN3_TTS_ATTN,
            "--stock-model",
            s.QWEN3_TTS_MODEL,
            "--design-model",
            s.QWEN3_TTS_DESIGN_MODEL,
            "--clone-model",
            s.QWEN3_TTS_CLONE_MODEL,
        ]

    def _ensure_worker(self) -> subprocess.Popen:
        """Return a live worker process, spawning one if needed."""
        if self._proc is not None and self._proc.poll() is None:
            return self._proc

        logger.info("Starting qwen3_tts worker: %s", self._worker_args())
        self._proc = subprocess.Popen(  # fixed argv, no shell
            self._worker_args(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        return self._proc

    def _request_sync(self, job: dict) -> dict:
        """Send one job and block until its (prefixed) response arrives."""
        with self._lock:
            for attempt in range(2):
                proc = self._ensure_worker()
                self._request_id += 1
                job = {**job, "id": self._request_id}
                try:
                    assert proc.stdin is not None and proc.stdout is not None
                    proc.stdin.write(json.dumps(job) + "\n")
                    proc.stdin.flush()
                    response = self._read_response(proc, self._request_id)
                except (BrokenPipeError, OSError, RuntimeError) as exc:
                    logger.warning("qwen3_tts worker died (attempt %d): %s", attempt + 1, exc)
                    self._proc = None
                    if attempt == 1:
                        raise EngineUnavailableError(
                            f"qwen3_tts worker no responde: {exc}"
                        ) from exc
                    continue

                if not response.get("ok"):
                    raise EngineUnavailableError(f"qwen3_tts falló: {response.get('error')}")
                return response

            raise EngineUnavailableError("qwen3_tts worker no responde")

    def _read_response(self, proc: subprocess.Popen, request_id: int) -> dict:
        """Read lines until the response for ``request_id`` is found."""
        assert proc.stdout is not None
        while True:
            line = proc.stdout.readline()
            if line == "":
                raise RuntimeError("worker process closed its stdout")
            if not line.startswith(RESPONSE_PREFIX):
                continue
            payload = json.loads(line[len(RESPONSE_PREFIX) :])
            if payload.get("id") == request_id:
                return payload

    async def _render(self, job: dict) -> bytes:
        """Run a job and return the produced WAV bytes."""
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            out_path = Path(tmp.name)
        job = {**job, "out": str(out_path)}
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._request_sync, job),
                timeout=_REQUEST_TIMEOUT_SEC,
            )
            return out_path.read_bytes()
        finally:
            out_path.unlink(missing_ok=True)

    # --- TtsEngineBase -----------------------------------------------------

    def _qwen_language(self, language: str) -> str:
        try:
            return LANGUAGE_NAMES[language]
        except KeyError:
            raise UnsupportedLanguageError(language, list(LANGUAGE_NAMES)) from None

    async def synthesize(
        self,
        *,
        text: str,
        voice_id: str,
        language: str,
        speed: float = 1.0,
        emotion: str | None = None,
        engine_name: str | None = None,
    ) -> bytes:
        """Qwen3-TTS exposes no stock presets; use clone/design instead."""
        raise FeatureNotSupportedError("stock voices", self.name)

    async def synthesize_clone(
        self,
        *,
        text: str,
        reference_audio_path: str,
        language: str,
        speed: float = 1.0,
        ref_text: str | None = None,
        engine_name: str | None = None,
    ) -> bytes:
        """Clone a voice from reference audio with the Base model."""
        if not Path(reference_audio_path).exists():
            raise EngineUnavailableError(f"Reference audio no encontrado: {reference_audio_path}")
        return await self._render(
            {
                "op": "clone",
                "text": text,
                "language": self._qwen_language(language),
                "ref_audio": reference_audio_path,
                "ref_text": ref_text,
            }
        )

    async def synthesize_instruct(
        self,
        *,
        text: str,
        instruct: str,
        language: str,
        speed: float = 1.0,
        emotion: str | None = None,
        engine_name: str | None = None,
    ) -> bytes:
        """Synthesize with a VoiceDesign instruct."""
        return await self._render(
            {
                "op": "design",
                "text": text,
                "language": self._qwen_language(language),
                "instruct": instruct,
            }
        )

    async def list_stock_voices(self, language: str | None = None) -> list[dict]:
        """Qwen3-TTS has no stock presets."""
        return []

    async def health_check(self) -> dict:
        runtime = self._python_bin().exists()
        worker_alive = self._proc is not None and self._proc.poll() is None
        return {
            # "model_loaded" doubles as runtime availability so a lazily-loaded
            # sub-engine does not flag a routed gateway as degraded.
            "model_loaded": runtime,
            "worker_alive": worker_alive,
            "gpu_available": self._settings.QWEN3_TTS_DEVICE.startswith("cuda"),
            "device": self._settings.QWEN3_TTS_DEVICE,
            "stock_voices_count": 0,
            "vram_free_mb": 0,
            "mode": "LOCAL" if runtime else "NOT_AVAILABLE",
            "engine": self.name,
        }
