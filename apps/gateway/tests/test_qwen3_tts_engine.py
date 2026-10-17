import pytest

from tts_api.core.engines.qwen3_tts_engine import Qwen3TTSEngine
from tts_api.core.exceptions import FeatureNotSupportedError, UnsupportedLanguageError


async def test_qwen3_tts_capabilities() -> None:
    e = Qwen3TTSEngine()
    c = e.capabilities
    assert c.stock_voices is False
    assert c.voice_cloning is True
    assert c.instruct_voice_design is True
    assert c.locations == ["local"]


async def test_qwen3_tts_unsupported_language() -> None:
    e = Qwen3TTSEngine()
    # Check language mapping first
    with pytest.raises(UnsupportedLanguageError):
        await e.synthesize_instruct(text="hi", instruct="female", language="xx")


async def test_qwen3_tts_stock_not_supported() -> None:
    e = Qwen3TTSEngine()
    with pytest.raises(FeatureNotSupportedError):
        await e.synthesize(text="hi", voice_id="bad", language="en")
