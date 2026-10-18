"""Line-protocol worker for Qwen3-TTS.

This module is executed as a standalone script by the *external* ``qwen-tts``
virtualenv (``QWEN3_TTS_PYTHON``), NOT imported by the API process: ``qwen-tts``
pins ``transformers==4.57.3``/``accelerate==1.12.0``, which conflict with the
versions the gateway process uses. Running it out-of-process keeps both worlds
isolated and lets the model stay warm across requests.

Protocol (one JSON object per line):

Request::

    {"id": 1, "op": "clone",  "text": "...", "language": "french",
     "ref_audio": "/path/ref.wav", "ref_text": "..." | null, "out": "/tmp/x.wav"}
    {"id": 2, "op": "design", "text": "...", "language": "french",
     "instruct": "...", "out": "/tmp/x.wav"}
    {"id": 3, "op": "custom", "text": "...", "language": "french",
     "speaker": "vivian", "instruct": null, "out": "/tmp/x.wav"}

Response lines are prefixed with :data:`RESPONSE_PREFIX` so that stray prints
from the model stack (transformers warnings, tqdm, etc.) can be ignored::

    @@QWEN3TTS@@{"id": 1, "ok": true, "sample_rate": 24000, "seconds": 4.5}
    @@QWEN3TTS@@{"id": 1, "ok": false, "error": "..."}
"""

from __future__ import annotations

import argparse
import gc
import json
import sys
import traceback
from typing import Any

RESPONSE_PREFIX = "@@QWEN3TTS@@"

#: One model resident at a time (each 1.7B model is ~3.5 GB in bf16).
_CACHE: dict[str, Any] = {"model": None, "kind": None}


def _emit(payload: dict[str, Any]) -> None:
    """Write one prefixed response line to stdout."""
    sys.stdout.write(RESPONSE_PREFIX + json.dumps(payload) + "\n")
    sys.stdout.flush()


def _repo_for(kind: str, args: argparse.Namespace) -> str:
    """Return the Hugging Face repo id for a model kind."""
    mapping: dict[str, str] = {
        "custom": args.stock_model,
        "design": args.design_model,
        "clone": args.clone_model,
    }
    return mapping[kind]


def _load(kind: str, args: argparse.Namespace) -> Any:
    """Load (or reuse) the model for ``kind``, evicting any other model."""
    if _CACHE["model"] is not None and _CACHE["kind"] == kind:
        return _CACHE["model"]

    import torch
    from qwen_tts import Qwen3TTSModel

    # Free the previous model before loading the next one.
    _CACHE["model"] = None
    _CACHE["kind"] = None
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    dtype = getattr(torch, args.dtype)
    _CACHE["model"] = Qwen3TTSModel.from_pretrained(
        _repo_for(kind, args),
        device_map=args.device,
        dtype=dtype,
        attn_implementation=args.attn,
    )
    _CACHE["kind"] = kind
    return _CACHE["model"]


def _handle(req: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    """Run one synthesis request and return the response payload."""
    op = req["op"]
    text = req["text"]
    language = req.get("language") or "auto"
    out = req["out"]

    if op == "clone":
        model = _load("clone", args)
        kwargs: dict[str, Any] = {"text": text, "language": language, "ref_audio": req["ref_audio"]}
        if req.get("ref_text"):
            kwargs["ref_text"] = req["ref_text"]
            kwargs["x_vector_only_mode"] = False
        else:
            kwargs["x_vector_only_mode"] = True
        wavs, sample_rate = model.generate_voice_clone(**kwargs)
    elif op == "design":
        model = _load("design", args)
        wavs, sample_rate = model.generate_voice_design(
            text=text,
            instruct=req["instruct"],
            language=language,
        )
    elif op == "custom":
        model = _load("custom", args)
        wavs, sample_rate = model.generate_custom_voice(
            text=text,
            speaker=req["speaker"],
            language=language,
            instruct=req.get("instruct") or None,
        )
    else:
        raise ValueError(f"unknown op: {op!r}")

    import soundfile as sf

    sf.write(out, wavs[0], sample_rate, format="WAV")
    return {
        "sample_rate": int(sample_rate),
        "seconds": len(wavs[0]) / float(sample_rate),
    }


def main() -> None:
    """Read requests from stdin and answer on stdout until EOF."""
    parser = argparse.ArgumentParser(description="Qwen3-TTS line-protocol worker")
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--dtype", default="bfloat16")
    parser.add_argument("--attn", default="sdpa")
    parser.add_argument("--stock-model", default="Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice")
    parser.add_argument("--design-model", default="Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign")
    parser.add_argument("--clone-model", default="Qwen/Qwen3-TTS-12Hz-1.7B-Base")
    args = parser.parse_args()

    # Keep third-party chatter on stderr so it cannot pollute the protocol.
    import os

    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            _emit({"id": None, "ok": False, "error": f"bad json: {exc}"})
            continue

        request_id = req.get("id")
        try:
            result = _handle(req, args)
            result.update({"id": request_id, "ok": True})
            _emit(result)
        except Exception as exc:  # report every failure to the caller
            _emit(
                {
                    "id": request_id,
                    "ok": False,
                    "error": str(exc),
                    "trace": traceback.format_exc()[-1500:],
                }
            )


if __name__ == "__main__":
    main()
