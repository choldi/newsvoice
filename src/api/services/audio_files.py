"""Shared filesystem primitives for cached audio files.

Both the digest TTS cache (``src/api/services/tts.py``) and the narration cache
(``src/api/services/narration_tts.py``) need the same two guarantees:

- a cached audio file is only reused when its metadata sidecar says the content
  is unchanged;
- a partially written audio file is never visible, so a crashed run cannot leave
  a truncated file that the next run would happily serve as a cache hit.

They live here so the hardening applied to one cache cannot silently diverge
from the other.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def sidecar_path(audio_path: Path) -> Path:
    """Return the metadata sidecar path for an audio file.

    Args:
        audio_path: Path of the cached audio file.

    Returns:
        Path of the sibling ``.meta.json`` file.
    """
    return audio_path.with_suffix(".meta.json")


def read_sidecar(path: Path) -> dict | None:
    """Read a cache sidecar, tolerating a missing or corrupted file.

    Args:
        path: Sidecar path.

    Returns:
        Parsed mapping, or None when absent, unreadable or malformed.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def atomic_write(path: Path, data: bytes) -> None:
    """Write bytes to a path atomically (temp file in the same dir + replace).

    Args:
        path: Destination path.
        data: Bytes to write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
