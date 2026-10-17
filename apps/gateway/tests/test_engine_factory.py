"""Tests for the engine factory."""

from __future__ import annotations

import pytest

from tts_api.core.engine_base import TtsEngineBase
from tts_api.core.engine_factory import _create_single_engine, create_engine


def test_create_single_engine_mock():
    """Test creating a mock engine."""
    engine = _create_single_engine("mock")
    assert isinstance(engine, TtsEngineBase)
    assert engine.name == "mock"


def test_create_single_engine_pocket_tts():
    """Test creating a pocket_tts engine."""
    engine = _create_single_engine("pocket_tts")
    assert isinstance(engine, TtsEngineBase)
    assert engine.name == "pocket_tts"


def test_create_single_engine_edgetts():
    """Test creating an edgetts engine."""
    engine = _create_single_engine("edgetts")
    assert isinstance(engine, TtsEngineBase)
    assert engine.name == "edgetts"


def test_create_single_engine_omnivoice():
    """Test creating an omnivoice engine."""
    engine = _create_single_engine("omnivoice")
    assert isinstance(engine, TtsEngineBase)
    assert engine.name == "omnivoice"


def test_create_single_engine_zono2():
    """Test creating a zono2 engine."""
    engine = _create_single_engine("zono2")
    assert isinstance(engine, TtsEngineBase)
    assert engine.name == "zono2"


def test_create_single_engine_kokoro():
    """Test creating a kokoro engine."""
    engine = _create_single_engine("kokoro")
    assert isinstance(engine, TtsEngineBase)
    assert engine.name == "kokoro"


def test_create_single_engine_piper():
    """Test creating a piper engine."""
    engine = _create_single_engine("piper")
    assert isinstance(engine, TtsEngineBase)
    assert engine.name == "piper"


def test_create_single_engine_chatterbox():
    """Test creating a chatterbox engine."""
    engine = _create_single_engine("chatterbox")
    assert isinstance(engine, TtsEngineBase)
    assert engine.name == "chatterbox"


def test_create_single_engine_unknown():
    """Test creating an unknown engine raises ValueError."""
    with pytest.raises(ValueError, match="Engine desconocido"):
        _create_single_engine("unknown_engine")


def test_create_single_engine_case_insensitive():
    """Test that engine names are case insensitive."""
    engine1 = _create_single_engine("ZONO2")
    engine2 = _create_single_engine("zono2")
    assert engine1.name == engine2.name == "zono2"


def test_create_engine_with_zono2_param():
    """Test creating engine with explicit parameter."""
    engine = create_engine("zono2")
    assert isinstance(engine, TtsEngineBase)
    assert engine.name == "zono2"


def test_create_engine_with_kokoro_param():
    """Test creating engine with explicit parameter."""
    engine = create_engine("kokoro")
    assert isinstance(engine, TtsEngineBase)
    assert engine.name == "kokoro"


def test_create_engine_with_piper_param():
    """Test creating engine with explicit parameter."""
    engine = create_engine("piper")
    assert isinstance(engine, TtsEngineBase)
    assert engine.name == "piper"


def test_create_engine_with_chatterbox_param():
    """Test creating engine with explicit parameter."""
    engine = create_engine("chatterbox")
    assert isinstance(engine, TtsEngineBase)
    assert engine.name == "chatterbox"
