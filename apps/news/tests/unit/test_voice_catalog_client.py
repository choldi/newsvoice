"""Unit tests for the HTTP client that reads the gateway's voice catalog (P6)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from src.api.models.voices import VoiceProfile
from src.api.services import voice_catalog_client
from src.api.services.voice_catalog_client import (
    DEFAULT_MAX_AGE,
    VoiceCatalogRequestError,
    cached_catalog,
    fetch_catalog,
    fetch_catalog_blocking,
    fetch_reference_audio,
    invalidate_catalog,
    prime_catalog,
    save_custom_profile,
    store_reference,
)
from src.core.voice_catalog import VoiceCatalogError, load_profiles
from tests.fake_gateway import (
    FakeGateway,
    current_gateway,
    invalidate_client,
    sample_wav,
)
from tests.fake_gateway import (
    save_custom_profile as seed_custom_profile,
)

#: The committed catalog, which the gateway ships and serves since P6.
CATALOG = Path(__file__).resolve().parents[3] / "gateway" / "voice_profiles.json"

PROFILE = {
    "name": "mi_voz_es",
    "label": "Mi voz",
    "language": "es",
    "instruct": "Femenina, 30s, tono cercano y agil, claridad de locucion",
    "ref_text": "Hola, soy tu profe de ingles de siempre. " * 4,
}


def _profile(name: str = "mi_voz_es") -> VoiceProfile:
    """Build a valid catalog profile for the client tests."""
    return VoiceProfile.model_validate(dict(PROFILE, name=name))


def _answer_with(status_code: int, body: object) -> None:
    """Point the client at a transport that always answers ``status_code``."""
    from src.api.services.gateway import GatewayClient

    transport = httpx.MockTransport(
        lambda request: httpx.Response(status_code, json=body, request=request)
    )
    client = httpx.AsyncClient(base_url="http://gateway.test/api/v1", transport=transport)
    voice_catalog_client._dispatcher = lambda: GatewayClient(client=client)


def _fail_transport(exc: Exception) -> None:
    """Point the blocking reader at a transport that never answers."""

    def boom(request: httpx.Request) -> httpx.Response:
        raise exc

    voice_catalog_client._blocking_http = lambda: httpx.Client(
        base_url="http://gateway.test/api/v1", transport=httpx.MockTransport(boom)
    )


# --- fetch_catalog -----------------------------------------------------------


async def test_fetch_catalog_serves_the_committed_profiles() -> None:
    snapshot = await fetch_catalog()

    assert len(snapshot.profiles) == 32
    assert snapshot.generated == []
    assert snapshot.custom == []


async def test_fetch_catalog_reuses_a_fresh_snapshot() -> None:
    first = await fetch_catalog()
    second = await fetch_catalog()

    assert first is second
    assert len(current_gateway().requests) == 1


async def test_fetch_catalog_with_no_max_age_always_refreshes() -> None:
    first = await fetch_catalog()
    second = await fetch_catalog(max_age=0)

    assert first is not second
    assert len(current_gateway().requests) == 2


async def test_fetch_catalog_keeps_the_gateway_message_on_an_outage() -> None:
    current_gateway().unavailable = 503

    with pytest.raises(VoiceCatalogRequestError, match="gateway unavailable") as excinfo:
        await fetch_catalog()

    assert excinfo.value.retryable is True


async def test_fetch_catalog_marks_a_refusal_as_not_retryable() -> None:
    _answer_with(404, {"detail": "no such endpoint"})

    with pytest.raises(VoiceCatalogRequestError, match="no such endpoint") as excinfo:
        await fetch_catalog()

    assert excinfo.value.retryable is False


async def test_fetch_catalog_rejects_a_body_that_is_not_a_catalog() -> None:
    _answer_with(200, {"profiles": "nope"})

    with pytest.raises(VoiceCatalogRequestError, match="unusable voice catalog") as excinfo:
        await fetch_catalog()

    assert excinfo.value.retryable is False


async def test_fetch_catalog_reports_a_body_without_json() -> None:
    from src.api.services.gateway import GatewayClient

    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"not json", request=request)
    )
    client = httpx.AsyncClient(base_url="http://gateway.test/api/v1", transport=transport)
    voice_catalog_client._dispatcher = lambda: GatewayClient(client=client)

    with pytest.raises(VoiceCatalogRequestError, match="without a JSON body") as excinfo:
        await fetch_catalog()

    assert excinfo.value.retryable is False


async def test_fetch_catalog_wraps_a_gateway_transport_failure() -> None:
    from src.api.services.gateway import GatewayClient

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    transport = httpx.MockTransport(boom)
    client = httpx.AsyncClient(base_url="http://gateway.test/api/v1", transport=transport)
    voice_catalog_client._dispatcher = lambda: GatewayClient(client=client)

    with pytest.raises(VoiceCatalogRequestError) as excinfo:
        await fetch_catalog()

    assert excinfo.value.retryable is True


# --- fetch_catalog_blocking --------------------------------------------------


def test_fetch_catalog_blocking_primes_the_snapshot_once() -> None:
    first = fetch_catalog_blocking()

    assert len(first.profiles) == 32
    assert len(current_gateway().requests) == 1

    second = fetch_catalog_blocking()

    assert second is first
    assert len(current_gateway().requests) == 1, "a warm reader never blocks on the gateway"


def test_fetch_catalog_blocking_reads_the_snapshot_an_async_reader_left() -> None:
    prime_catalog(fetch_catalog_blocking())
    before = len(current_gateway().requests)

    snapshot = fetch_catalog_blocking()

    assert snapshot is cached_catalog()
    assert len(current_gateway().requests) == before


def test_fetch_catalog_blocking_reports_an_unreachable_gateway() -> None:
    current_gateway().unavailable = 503

    with pytest.raises(VoiceCatalogRequestError, match="gateway unavailable"):
        fetch_catalog_blocking()


def test_fetch_catalog_blocking_reports_a_transport_failure() -> None:
    _fail_transport(httpx.ConnectError("connection refused"))

    with pytest.raises(VoiceCatalogRequestError, match="failed") as excinfo:
        fetch_catalog_blocking()

    assert excinfo.value.retryable is True


def test_synchronous_readers_serve_the_snapshot() -> None:
    """``load_profiles`` with no path is the blocking reader of the snapshot."""
    seed_custom_profile(_profile())

    names = [profile.name for profile in load_profiles()]

    assert names[-1] == "mi_voz_es"
    assert len(names) == 33


# --- fetch_reference_audio ---------------------------------------------------


async def test_fetch_reference_audio_returns_the_stored_wav() -> None:
    profile = _profile()
    seed_custom_profile(profile)
    wav = sample_wav()
    current_gateway().add_reference(
        profile.name, wav, instruct=profile.instruct, ref_text=profile.ref_text
    )

    assert await fetch_reference_audio("mi_voz_es.wav") == wav
    assert await fetch_reference_audio("mi_voz_es") == wav


async def test_fetch_reference_audio_is_none_when_there_is_no_reference() -> None:
    assert await fetch_reference_audio("anchor_neutral_m") is None


async def test_fetch_reference_audio_keeps_the_gateway_outage_retryable() -> None:
    current_gateway().unavailable = 503

    with pytest.raises(VoiceCatalogRequestError, match="gateway unavailable") as excinfo:
        await fetch_reference_audio("anchor_neutral_m")

    assert excinfo.value.retryable is True


# --- store_reference ---------------------------------------------------------


async def test_store_reference_measures_the_audio_the_gateway_received() -> None:
    profile = _profile()
    seed_custom_profile(profile)
    invalidate_client()
    wav = sample_wav()

    entry = await store_reference(
        profile.name,
        wav,
        instruct=profile.instruct,
        ref_text=profile.ref_text,
        design_model="Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
    )

    assert entry.file == "refs/mi_voz_es.wav"
    assert entry.sample_rate == 24000
    assert entry.duration_seconds > 0
    assert current_gateway().refs["mi_voz_es"] == wav
    assert cached_catalog() is None, "a write drops the snapshot the next read refreshes"


async def test_store_reference_rejects_an_unknown_profile() -> None:
    with pytest.raises(VoiceCatalogRequestError, match="unknown voice profile") as excinfo:
        await store_reference("no_existe", sample_wav(), instruct="instruct", ref_text="ref text")

    assert excinfo.value.retryable is False


async def test_store_reference_keeps_the_gateway_outage_retryable() -> None:
    current_gateway().unavailable = 503

    with pytest.raises(VoiceCatalogRequestError, match="gateway unavailable") as excinfo:
        await store_reference(
            "anchor_neutral_m", sample_wav(), instruct="instruct", ref_text="ref text"
        )

    assert excinfo.value.retryable is True


async def test_store_reference_rejects_unusable_audio() -> None:
    with pytest.raises(VoiceCatalogError, match="not a readable WAV"):
        await store_reference(
            "anchor_neutral_m", b"not a wav", instruct="instruct", ref_text="ref text"
        )


# --- save_custom_profile -----------------------------------------------------


async def test_save_custom_profile_appends_and_invalidates() -> None:
    saved = await save_custom_profile(_profile())

    assert saved.name == "mi_voz_es"
    assert [p.name for p in fetch_catalog_blocking().profiles][-1] == "mi_voz_es"
    assert len(current_gateway().custom) == 1


async def test_save_custom_profile_rejects_a_taken_name() -> None:
    await save_custom_profile(_profile())

    with pytest.raises(VoiceCatalogRequestError, match="already exists") as excinfo:
        await save_custom_profile(_profile())

    assert excinfo.value.retryable is False


async def test_save_custom_profile_rejects_a_committed_name() -> None:
    committed = load_profiles(CATALOG)[0].name

    with pytest.raises(VoiceCatalogRequestError, match="already in the catalog") as excinfo:
        await save_custom_profile(_profile(committed))

    assert excinfo.value.retryable is False


# --- snapshot helpers --------------------------------------------------------


async def test_prime_and_invalidate_the_snapshot() -> None:
    snapshot = await fetch_catalog()
    prime_catalog(snapshot)
    assert cached_catalog() is snapshot

    invalidate_catalog()
    assert cached_catalog() is None


def test_default_max_age_is_the_documented_budget() -> None:
    assert DEFAULT_MAX_AGE == 15.0


def test_request_error_is_a_catalog_error() -> None:
    assert issubclass(VoiceCatalogRequestError, VoiceCatalogError)
    assert VoiceCatalogRequestError("boom").retryable is True


def test_fake_gateway_type_is_the_installed_one() -> None:
    assert isinstance(current_gateway(), FakeGateway)
