"""Wall-clock estimate of a TTS synthesis (Phase 8.5).

``duration_estimate`` answers "how long is the audio going to be"; this module
answers "how long am I going to wait". The two are different quantities: a
300-page book is hours of audio but minutes of work on four parallel Edge
streams, and a re-run over a warm chunk cache is seconds of work for the same
hours of audio.

Cached chunks cost nothing, which is the whole point of counting them
separately: a naive ``chars / rate`` would keep promising the full duration on
a resumed run, when in fact only the missing chunks are left to synthesize.

The rates themselves live in settings (``TTS_EDGE_CHARS_PER_SECOND`` and
friends) and are calibrated from ``audio_jobs.elapsed_seconds``, the measured
truth already recorded for every run.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from src.api.models.narration import TTSBackend
from src.api.services.tts_backends import resolve_backend, wall_seconds_for
from src.core.config import get_settings


@dataclass(frozen=True)
class SynthesisEstimate:
    """How long one synthesis is expected to take, and why.

    Attributes:
        backend: Engine the estimate is for.
        total_chars: Characters of speech text in the whole piece.
        chunk_count: Chunks the text is split into.
        cached_chunks: Chunks already on disk (cost nothing).
        uncached_chunks: Chunks that still have to be synthesized.
        uncached_chars: Characters in those chunks.
        concurrency: Parallel streams the estimate assumes.
        wall_seconds: Expected wall-clock seconds for the remaining work.
        audio_duration_seconds: Length of the audio that will exist afterwards
            (the quantity the older ``duration_estimate`` reports).
    """

    backend: str
    total_chars: int
    chunk_count: int
    cached_chunks: int
    uncached_chunks: int
    uncached_chars: int
    concurrency: int
    wall_seconds: float
    audio_duration_seconds: float

    def as_dict(self) -> dict[str, Any]:
        """Return the estimate as a plain JSON-serializable dict.

        Returns:
            Every field, ready for the job metadata sidecar.
        """
        return asdict(self)


def estimate_synthesis(
    chunks: Sequence[str],
    *,
    backend: TTSBackend | str = TTSBackend.EDGE,
    concurrency: int = 1,
    is_cached: Callable[[str], bool] | None = None,
) -> SynthesisEstimate:
    """Estimate the wall-clock cost of synthesizing ``chunks``.

    Args:
        chunks: Speech text already split the way the synthesizer will split it.
        backend: Engine that will do the work.
        concurrency: Parallel streams the synthesizer uses.
        is_cached: Predicate answering whether a chunk is already on disk;
            cached chunks are excluded from the cost.

    Returns:
        The estimate, including the audio length for comparison with
        ``duration_estimate``.
    """
    from src.api.services.tts import estimate_duration_seconds

    engine = resolve_backend(backend)
    settings = get_settings()
    total_chars = sum(len(chunk) for chunk in chunks)
    pending = [chunk for chunk in chunks if not (is_cached is not None and is_cached(chunk))]
    uncached_chars = sum(len(chunk) for chunk in pending)
    parallel = max(1, int(concurrency))

    wall = wall_seconds_for(engine, uncached_chars) / parallel
    if pending:
        wall += len(pending) * settings.tts_chunk_overhead_seconds
        wall += settings.tts_join_overhead_seconds

    return SynthesisEstimate(
        backend=str(engine),
        total_chars=total_chars,
        chunk_count=len(chunks),
        cached_chunks=len(chunks) - len(pending),
        uncached_chunks=len(pending),
        uncached_chars=uncached_chars,
        concurrency=parallel,
        wall_seconds=round(max(0.0, wall), 2),
        audio_duration_seconds=estimate_duration_seconds(total_chars),
    )


def eta_seconds(estimate: dict[str, Any] | None, progress: dict[str, Any] | None) -> float | None:
    """Turn persisted estimate + progress into a live "time remaining".

    While chunks are coming in, the measured rate beats the modelled one: the
    first chunk already carries the real engine speed of this host, the real
    chunk sizes and the real contention. Before the first chunk there is nothing
    to measure, so the model's own number is the best available answer.

    Args:
        estimate: The ``SynthesisEstimate.as_dict()`` stored in the job meta.
        progress: ``{'done', 'total', 'elapsed_seconds'}`` written during
            synthesis.

    Returns:
        Seconds remaining, or None when neither a forecast nor a measurement
        exists yet (nothing has been extracted and nothing synthesized).
    """
    forecast = (estimate or {}).get("wall_seconds")
    total = int((progress or {}).get("total") or 0)
    done = int((progress or {}).get("done") or 0)
    elapsed = float((progress or {}).get("elapsed_seconds") or 0.0)
    if total > 0 and done >= total:
        return 0.0
    if done > 0 and elapsed > 0:
        return round(elapsed / done * (total - done), 1)
    if isinstance(forecast, (int, float)):
        return float(forecast)
    return None
