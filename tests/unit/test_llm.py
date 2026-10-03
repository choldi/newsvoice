"""Unit tests for src/core/llm.py wrapper."""

from unittest.mock import AsyncMock, patch

import pytest

from src.core.llm import generate_completion


@pytest.mark.asyncio
async def test_generate_completion_success() -> None:
    """Tests successful completion generation using mocked LiteLLM acompletion."""
    mock_choice = AsyncMock()
    mock_choice.message.content = "Mocked Markdown response"
    mock_response = AsyncMock()
    mock_response.choices = [mock_choice]

    with patch("litellm.acompletion", return_value=mock_response) as mock_acompletion:
        messages = [{"role": "user", "content": "Hello"}]
        result = await generate_completion(messages=messages)

        assert result == "Mocked Markdown response"
        mock_acompletion.assert_called_once()
        kwargs = mock_acompletion.call_args.kwargs
        assert kwargs["max_tokens"] > 0
        assert kwargs["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}


@pytest.mark.asyncio
async def test_generate_completion_does_not_disable_thinking_when_configured() -> None:
    """When llm_disable_thinking is off, no extra_body is sent."""
    mock_choice = AsyncMock()
    mock_choice.message.content = "Response"
    mock_response = AsyncMock()
    mock_response.choices = [mock_choice]

    with (
        patch("litellm.acompletion", return_value=mock_response) as mock_acompletion,
        patch("src.core.llm.settings") as mock_settings,
    ):
        mock_settings.llm_model = "openai/local-model"
        mock_settings.llm_api_base = "http://localhost:8080/v1"
        mock_settings.llm_temperature = 0.3
        mock_settings.llm_timeout_seconds = 1800
        mock_settings.llm_max_tokens = 4096
        mock_settings.llm_disable_thinking = False
        await generate_completion(messages=[{"role": "user", "content": "Hello"}])

    kwargs = mock_acompletion.call_args.kwargs
    assert "extra_body" not in kwargs
    assert kwargs["max_tokens"] == 4096


@pytest.mark.asyncio
async def test_generate_completion_failure() -> None:
    """Tests exception handling when LiteLLM completion fails."""
    with patch("litellm.acompletion", side_effect=Exception("Connection refused")):
        messages = [{"role": "user", "content": "Hello"}]
        with pytest.raises(RuntimeError) as exc_info:
            await generate_completion(messages=messages)

        assert "LLM generation failed" in str(exc_info.value)


@pytest.mark.asyncio
async def test_generate_completion_uses_the_empty_placeholder_key() -> None:
    """No configured key still sends api_key: litellm refuses to run without one.

    Newer litellm (1.103) raises 'Missing credentials' even for a local
    llama.cpp server, so an empty key becomes the conventional 'EMPTY'
    placeholder instead of being omitted.
    """
    mock_choice = AsyncMock()
    mock_choice.message.content = "hola"
    mock_response = AsyncMock()
    mock_response.choices = [mock_choice]

    with (
        patch("litellm.acompletion", return_value=mock_response) as mock_acompletion,
        patch("src.core.llm.settings") as mock_settings,
    ):
        mock_settings.llm_model = "openai/local-model"
        mock_settings.llm_api_base = "http://localhost:8080/v1"
        mock_settings.llm_temperature = 0.3
        mock_settings.llm_timeout_seconds = 1800
        mock_settings.llm_max_tokens = 4096
        mock_settings.llm_disable_thinking = True
        mock_settings.llm_api_key = ""
        await generate_completion(messages=[{"role": "user", "content": "hola"}])

    kwargs = mock_acompletion.call_args.kwargs
    assert kwargs["api_key"] == "EMPTY"


@pytest.mark.asyncio
async def test_generate_completion_passes_an_explicit_key_verbatim() -> None:
    """An explicit api_key argument wins over settings and the placeholder."""
    mock_choice = AsyncMock()
    mock_choice.message.content = "ok"
    mock_response = AsyncMock()
    mock_response.choices = [mock_choice]

    with patch("litellm.acompletion", return_value=mock_response) as mock_acompletion:
        await generate_completion(messages=[{"role": "user", "content": "hola"}], api_key="sk-real")

    kwargs = mock_acompletion.call_args.kwargs
    assert kwargs["api_key"] == "sk-real"


@pytest.mark.asyncio
async def test_generate_completion_falls_back_to_the_settings_key() -> None:
    """LLM_API_KEY from the environment is used when no argument is given."""
    mock_choice = AsyncMock()
    mock_choice.message.content = "ok"
    mock_response = AsyncMock()
    mock_response.choices = [mock_choice]

    with (
        patch("litellm.acompletion", return_value=mock_response) as mock_acompletion,
        patch("src.core.llm.settings") as mock_settings,
    ):
        mock_settings.llm_model = "openai/local-model"
        mock_settings.llm_api_base = "http://localhost:8080/v1"
        mock_settings.llm_temperature = 0.3
        mock_settings.llm_timeout_seconds = 1800
        mock_settings.llm_max_tokens = 4096
        mock_settings.llm_disable_thinking = True
        mock_settings.llm_api_key = "sk-settings"
        await generate_completion(messages=[{"role": "user", "content": "hola"}])

    kwargs = mock_acompletion.call_args.kwargs
    assert kwargs["api_key"] == "sk-settings"
