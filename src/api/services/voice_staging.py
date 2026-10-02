"""Staging area for reference voices designed on the GPU box (Phase 7.9).

The design worker runs on another machine, so the audio it produces cannot
reach ``data/voices`` through Temporal: a 24-voice batch put ~20 MB of gzipped
WAV into one workflow history and the server terminated it with
``Workflow history size exceeds limit``. Instead the design activity POSTs the
bytes here, the workflow carries only the digest, and the store activity reads
the staged file back from the directory the API and the catalog worker share.

The endpoint fails closed: with no ``voice_staging_api_key`` configured it
answers 503 and nothing can be written to disk.
"""

from __future__ import annotations

import gzip
import re
from pathlib import Path

import httpx

from src.core.config import settings

#: Profile names are snake_case; the pattern is what keeps a path out of the
#: payload, so a crafted name can never escape ``.staging``.
NAME_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")

#: A reference is at most a minute of 24 kHz mono audio, gzipped.
MAX_STAGED_BYTES = 20 * 1024 * 1024

#: Header the design worker authenticates with.
STAGING_KEY_HEADER = "X-Voice-Staging-Key"

#: How long the design worker waits for the server to take the bytes.
STAGING_TIMEOUT_SECONDS = 60.0


class VoiceStagingError(Exception):
    """A staged payload could not be written, read or shipped.

    Attributes:
        retryable: False when retrying cannot change the outcome (bad key,
            missing configuration, invalid name).
    """

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


def staging_dir() -> Path:
    """Return the directory staged references are written to."""
    return Path(settings.voices_dir) / ".staging"


def staged_path(name: str) -> Path:
    """Return the staged file for a profile, validating its name.

    Args:
        name: Profile name (snake_case).

    Returns:
        Path of the gzipped staged reference.

    Raises:
        VoiceStagingError: When the name is not a plain catalog identifier.
    """
    if not NAME_PATTERN.match(name):
        raise VoiceStagingError(f"invalid voice name: {name!r}", retryable=False)
    return staging_dir() / f"{name}.wav.gz"


def write_staged(name: str, gzipped: bytes) -> int:
    """Write a gzipped reference for the store activity to pick up.

    Args:
        name: Profile name.
        gzipped: Gzip stream of the WAV bytes.

    Returns:
        Number of bytes written.

    Raises:
        VoiceStagingError: When the payload is empty, oversized or the name
            is not a valid profile name.
    """
    path = staged_path(name)
    if not gzipped:
        raise VoiceStagingError(f"empty payload for {name}", retryable=False)
    if len(gzipped) > MAX_STAGED_BYTES:
        raise VoiceStagingError(
            f"payload for {name} exceeds {MAX_STAGED_BYTES} bytes", retryable=False
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzipped)
    return len(gzipped)


def read_staged(name: str) -> bytes:
    """Read back the WAV the design activity staged.

    Args:
        name: Profile name.

    Returns:
        The original WAV bytes.

    Raises:
        VoiceStagingError: When nothing was staged or the payload is not a
            gzip stream. Not retryable: the design step would have to run
            again, which is its own activity.
    """
    path = staged_path(name)
    if not path.is_file():
        raise VoiceStagingError(f"no staged audio for {name}", retryable=False)
    try:
        return gzip.decompress(path.read_bytes())
    except (OSError, EOFError) as exc:
        raise VoiceStagingError(
            f"staged audio for {name} is corrupt: {exc}", retryable=False
        ) from exc


def clear_staged(name: str) -> None:
    """Remove the staged reference for a profile, if any.

    Args:
        name: Profile name.
    """
    staged_path(name).unlink(missing_ok=True)


async def stage_from_design(name: str, wav: bytes) -> int:
    """Ship a freshly designed reference to the server over HTTP.

    Args:
        name: Profile name.
        wav: The synthesised WAV bytes.

    Returns:
        Number of gzipped bytes sent.

    Raises:
        VoiceStagingError: When staging is not configured, the server rejects
            the key, or the request fails. ``retryable`` tells the activity
            whether another attempt could work.
    """
    if not NAME_PATTERN.match(name):
        raise VoiceStagingError(f"invalid voice name: {name!r}", retryable=False)
    key = settings.voice_staging_api_key
    base = settings.voice_staging_url.rstrip("/")
    if not key:
        raise VoiceStagingError("voice_staging_api_key is not configured", retryable=False)
    if not base:
        raise VoiceStagingError("voice_staging_url is not configured", retryable=False)

    gzipped = gzip.compress(wav)
    url = f"{base}/api/internal/voices/staged/{name}"
    try:
        async with httpx.AsyncClient(timeout=STAGING_TIMEOUT_SECONDS) as client:
            response = await client.post(
                url,
                content=gzipped,
                headers={STAGING_KEY_HEADER: key, "Content-Type": "application/gzip"},
            )
    except httpx.HTTPError as exc:
        raise VoiceStagingError(f"staging request for {name} failed: {exc}") from exc

    body = response.text[:200]
    if response.status_code >= 400:
        # A 4xx means the key or the payload is wrong, which no retry can fix;
        # 408/429 are the exceptions that explicitly ask for another go.
        retryable = response.status_code in (408, 429) or response.status_code >= 500
        raise VoiceStagingError(
            f"staging request for {name} failed with {response.status_code}: {body}",
            retryable=retryable,
        )
    return len(gzipped)
