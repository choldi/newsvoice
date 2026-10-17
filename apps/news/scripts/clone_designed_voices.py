"""Clone the designed voices from data/voices/refs into the OmniVoice gateway.

Phase 7.9/7.12 tool: `data/voices` is git-ignored, so after a migration the
designed references live only wherever the design ran. Copy that directory
here (refs + manifest.json) and run this script to register every designed
voice in the gateway the GUI and the cast editors read.

Idempotent (names already in the gateway are skipped). Refs longer than 30 s
(the gateway limit) are trimmed to their first 30 s; every wav is verified
against the manifest sha256 first, and `ref_text` is only sent when it matches
the manifest's `ref_text_sha256`.

Usage (from the repository root, so .env is found):

    .venv/bin/python scripts/clone_designed_voices.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.api.services.voices import VoicesService  # noqa: E402

MAX_SECONDS = 30.0
MIN_SECONDS = 5.0


def trim_wav(src: Path, seconds: float) -> bytes:
    """Return the first `seconds` of a WAV file via ffmpeg."""
    with tempfile.NamedTemporaryFile(suffix=".wav") as out:
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-t", str(seconds), out.name],
            check=True,
        )
        return Path(out.name).read_bytes()


def enabled(profile: dict) -> bool:
    value = profile.get("enabled", True)
    return value if isinstance(value, bool) else str(value).strip().lower() in ("true", "1", "yes")


async def main() -> int:
    profiles = json.loads((ROOT / "voice_profiles.json").read_text())["profiles"]
    languages = json.loads((ROOT / "voice_profiles.json").read_text())["languages"]
    manifest = json.loads((ROOT / "data/voices/manifest.json").read_text())
    seg_by_lang = {lang: " ".join(d["ref_segments"]) for lang, d in languages.items()}

    service = VoicesService()
    existing = {c.get("name") for c in await service.list_cloned()}
    print(f"gateway: {len(existing)} clones existentes | {len(profiles)} perfiles")

    results: list[tuple[str, str]] = []
    for profile in profiles:
        name = profile["name"]
        if not enabled(profile):
            results.append((name, "SKIP (deshabilitado)"))
            continue
        if name in existing:
            results.append((name, "SKIP (ya clonada)"))
            continue
        entry = manifest.get(name) or {}
        wav = ROOT / f"data/voices/refs/{name}.wav"
        if not wav.exists():
            results.append((name, "FALTA REF"))
            continue
        digest = hashlib.sha256(wav.read_bytes()).hexdigest()
        if digest != entry.get("sha256"):
            results.append((name, "SHA256 NO CUADRA (ref corrupta)"))
            continue
        duration = float(entry.get("duration_seconds") or 0)
        if duration < MIN_SECONDS:
            results.append((name, f"REF CORTA ({duration:.1f}s)"))
            continue
        audio = trim_wav(wav, MAX_SECONDS) if duration > MAX_SECONDS else wav.read_bytes()
        ref_text = seg_by_lang.get(profile.get("language"))
        if ref_text and hashlib.sha256(ref_text.encode()).hexdigest() != entry.get(
            "ref_text_sha256"
        ):
            ref_text = None
        try:
            response = await service.clone_voice(
                name=name,
                language=str(profile.get("language") or "en"),
                reference_audio=audio,
                filename=f"{name}.wav",
                ref_text=ref_text,
            )
            voice_id = str(response.get("voice_id") or response.get("id") or "?")
            tag = "OK"
            if duration > MAX_SECONDS:
                tag += f" (recortada a {MAX_SECONDS:.0f}s)"
            results.append((name, f"{tag} -> {voice_id[:16]}"))
        except Exception as exc:  # noqa: BLE001 - one bad voice must not stop the batch
            results.append((name, f"FALLO: {str(exc)[:120]}"))

    ok = sum(1 for _, r in results if r.startswith("OK"))
    skip = sum(1 for _, r in results if r.startswith("SKIP"))
    bad = [name for name, r in results if not r.startswith(("OK", "SKIP"))]
    for name, result in results:
        print(f"  {name:28s} {result}")
    print(f"---\nOK: {ok} | saltadas: {skip} | problemas: {len(bad)}")
    final = await service.list_cloned()
    print(f"clones en el gateway: {len(final)}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
