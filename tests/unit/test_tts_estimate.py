"""Unit tests for the Phase 8.5 wall-clock synthesis estimate."""

import pytest

from src.api.models.narration import TTSBackend
from src.api.services.tts_backends import wall_seconds_for
from src.api.services.tts_estimate import estimate_synthesis, eta_seconds
from src.core.config import settings


def _chunks(count: int, size: int = 400) -> list[str]:
    """Build a predictable chunk list."""
    return [f"chunk {index} " + "x" * size for index in range(count)]


@pytest.fixture(autouse=True)
def _rates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the throughput model so the assertions are about the formula."""
    monkeypatch.setattr(settings, "tts_edge_chars_per_second", 40.0)
    monkeypatch.setattr(settings, "tts_gateway_chars_per_second", 8.0)
    monkeypatch.setattr(settings, "tts_chunk_overhead_seconds", 0.0)
    monkeypatch.setattr(settings, "tts_join_overhead_seconds", 0.0)


def test_estimate_divides_the_work_by_concurrency() -> None:
    """Four streams take a quarter of the serial time."""
    chunks = _chunks(40)

    serial = estimate_synthesis(chunks, backend="edge", concurrency=1)
    parallel = estimate_synthesis(chunks, backend="edge", concurrency=4)

    assert parallel.wall_seconds == pytest.approx(serial.wall_seconds / 4, abs=0.01)
    assert serial.wall_seconds > 0


def test_cached_chunks_cost_nothing() -> None:
    """A resumed run is priced from the missing chunks only."""
    chunks = _chunks(20)
    full = estimate_synthesis(chunks, backend="edge")
    half = estimate_synthesis(chunks, backend="edge", is_cached=lambda c: c in chunks[:10])

    assert half.cached_chunks == 10
    assert half.uncached_chunks == 10
    assert half.uncached_chars < full.uncached_chars
    assert half.wall_seconds < full.wall_seconds
    assert half.total_chars == full.total_chars, "the audio length is unchanged"


def test_a_fully_cached_render_costs_zero_seconds() -> None:
    """Everything on disk means nothing left to wait for."""
    chunks = _chunks(12)

    estimate = estimate_synthesis(chunks, backend="edge", is_cached=lambda _c: True)

    assert estimate.wall_seconds == 0.0
    assert estimate.uncached_chunks == 0
    assert estimate.audio_duration_seconds > 0, "the audio still exists"


def test_estimate_grows_with_the_remaining_text() -> None:
    """Twice the missing characters is (at least) twice the wait."""
    chunks = _chunks(10)
    small = estimate_synthesis(chunks[:5], backend="edge")
    large = estimate_synthesis(chunks, backend="edge")

    assert large.wall_seconds > small.wall_seconds


def test_audio_duration_keeps_the_legacy_formula() -> None:
    """duration_estimate is chars/15 and must not drift while it coexists."""
    estimate = estimate_synthesis(["x" * 1500], backend="edge")

    assert estimate.audio_duration_seconds == 100.0


def test_the_backend_decides_the_rate() -> None:
    """The gateway is modelled slower than edge, so it must cost more."""
    chunks = _chunks(10)

    edge = estimate_synthesis(chunks, backend="edge")
    gateway = estimate_synthesis(chunks, backend=TTSBackend.OMNIVOICE)

    assert gateway.wall_seconds > edge.wall_seconds
    assert gateway.backend == "omni_voice"


def test_unknown_backend_is_rejected() -> None:
    """A typo in TTS_DEFAULT_BACKEND must not silently price as edge."""
    from src.api.services.tts_backends import TTSBackendError

    with pytest.raises(TTSBackendError):
        estimate_synthesis(_chunks(1), backend="siri")


# --- wall_seconds_for --------------------------------------------------------


def test_edge_and_gateway_are_linear_throughputs(monkeypatch: pytest.MonkeyPatch) -> None:
    """1000 chars at 40/s is 25 s, at 8/s is 125 s."""
    assert wall_seconds_for(TTSBackend.EDGE, 1000) == pytest.approx(25.0)
    assert wall_seconds_for(TTSBackend.OMNIVOICE, 1000) == pytest.approx(125.0)


def test_modal_reuses_the_budget_model() -> None:
    """Time and cost must come from the same model, or they will disagree."""
    from src.core.budget import estimate_gpu_seconds

    assert wall_seconds_for(TTSBackend.QWEN3_TTS, 5000) == pytest.approx(estimate_gpu_seconds(5000))


def test_negative_work_costs_nothing() -> None:
    """An empty chunk list is not a negative duration."""
    assert wall_seconds_for(TTSBackend.EDGE, 0) == 0.0
    assert wall_seconds_for(TTSBackend.EDGE, -10) == 0.0


# --- eta_seconds -------------------------------------------------------------


def test_eta_uses_the_measured_rate_once_chunks_finish() -> None:
    """Ten chunks in 100 s means the remaining ninety take 900 s."""
    estimate = {"wall_seconds": 5000.0}
    progress = {"done": 10, "total": 100, "elapsed_seconds": 100.0}

    assert eta_seconds(estimate, progress) == pytest.approx(900.0, abs=0.1)


def test_eta_falls_back_to_the_forecast_before_the_first_chunk() -> None:
    """Nothing measured yet, so the model is the best available answer."""
    assert eta_seconds({"wall_seconds": 123.0}, {"done": 0, "total": 10}) == 123.0


def test_eta_is_zero_once_every_chunk_is_done() -> None:
    """A finished render does not promise more waiting."""
    assert eta_seconds({"wall_seconds": 123.0}, {"done": 10, "total": 10}) == 0.0


def test_eta_falls_back_to_the_forecast_when_nothing_is_measured() -> None:
    """Extraction has priced the render but no chunk has finished yet."""
    assert eta_seconds({"wall_seconds": 123.0}, None) == 123.0
    assert eta_seconds({"wall_seconds": 123.0}, {"done": 0, "total": 0}) == 123.0


def test_eta_is_none_when_nothing_is_known() -> None:
    """No forecast and no progress: there is simply nothing to say."""
    assert eta_seconds(None, {"done": 3, "total": 0}) is None
    assert eta_seconds(None, None) is None
    assert eta_seconds({}, {}) is None
