"""Tests for the declarative designed-voice setup flow."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from setup_wizard import config_report
from setup_wizard.design_voices import (
    BasicVoice,
    VoiceCreationOptions,
    create_basic_voices,
    load_basic_voices,
)
from setup_wizard.settings import ENGINE_INFO

_SAMPLE = BasicVoice(
    name="basic_es_female",
    language="es",
    instruct="Voz femenina española, cálida y clara, con ritmo de locución de noticias.",
    label="Español · femenina",
    ref_text="Buenos días. Estas son las noticias más importantes del día.",
    role_hint="anchor",
)


class _Response:
    def __init__(self, payload: Any) -> None:
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False


def _install_fake_http(monkeypatch: Any, existing: bool = False) -> list[Any]:
    calls: list[Any] = []

    def fake_urlopen(request: Any, timeout: float = 30.0) -> _Response:
        calls.append(request)
        url = request.full_url
        if request.method == "GET" and url.endswith("/voices/designed?limit=1000"):
            return _Response([{"name": _SAMPLE.name}] if existing else [])
        if request.method == "GET" and "/api/admin/voices/all" in url:
            return _Response({"voices": [{"id": _SAMPLE.name}] if existing else []})
        return _Response({"voice_id": "designed-id", "workflow_id": "wf-id", "start_error": ""})

    monkeypatch.setattr("setup_wizard.design_voices.urlopen", fake_urlopen)
    return calls


def test_manifest_declares_basic_voices() -> None:
    voices = load_basic_voices()
    assert [voice.name for voice in voices] == [
        "basic_es_female",
        "basic_es_male",
        "basic_en_female",
        "basic_en_male",
    ]
    assert all(len(voice.instruct) >= 10 for voice in voices)
    assert all(voice.ref_text for voice in voices)


def test_manifest_rejects_invalid_slug(tmp_path: Path) -> None:
    manifest = tmp_path / "voices.toml"
    manifest.write_text(
        '[[voices]]\nname = "Bad Name"\nlanguage = "es"\n'
        'instruct = "Voz clara para informativos"\nref_text = "Texto"\n',
        encoding="utf-8",
    )
    try:
        load_basic_voices(manifest)
    except ValueError as exc:
        assert "Nombre de voz inválido" in str(exc)
    else:
        raise AssertionError("Expected ValueError for an invalid slug")


def test_design_capability_comes_from_registry() -> None:
    assert ENGINE_INFO["qwen3_tts"].voice_design is True
    assert ENGINE_INFO["omnivoice_remote"].voice_design is True
    assert ENGINE_INFO["pocket_tts"].voice_design is False
    assert "| Design |" in config_report._engine_table(["qwen3_tts"], {})[0]


def test_create_basic_voices_calls_both_flows(monkeypatch: Any) -> None:
    calls = _install_fake_http(monkeypatch)
    options = VoiceCreationOptions(
        gateway_key="gw-secret",
        admin_token="admin-secret",
        design_engine="qwen3_tts",
    )
    results = create_basic_voices(options, [_SAMPLE])
    assert results[0].gateway.startswith("creada")
    assert results[0].news.startswith("creada")
    posts = [request for request in calls if request.method == "POST"]
    assert len(posts) == 2
    assert posts[0].full_url.endswith("/voices/design")
    assert posts[1].full_url.endswith("/api/admin/voices/custom")
    assert json.loads(posts[0].data)["engine"] == "qwen3_tts"
    assert json.loads(posts[1].data)["run_in_modal"] is False


def test_create_basic_voices_is_idempotent(monkeypatch: Any) -> None:
    calls = _install_fake_http(monkeypatch, existing=True)
    results = create_basic_voices(VoiceCreationOptions(), [_SAMPLE])
    assert results[0].gateway == "ya existe"
    assert results[0].news == "ya existe"
    assert not [request for request in calls if request.method == "POST"]


def test_dry_run_does_not_call_mutating_endpoints(monkeypatch: Any) -> None:
    calls = _install_fake_http(monkeypatch)
    results = create_basic_voices(VoiceCreationOptions(dry_run=True), [_SAMPLE])
    assert results[0].gateway == "simulada"
    assert results[0].news == "simulada"
    assert not calls
