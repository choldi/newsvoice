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


@pytest.mark.asyncio
async def test_generate_completion_failure() -> None:
    """Tests exception handling when LiteLLM completion fails."""
    with patch("litellm.acompletion", side_effect=Exception("Connection refused")):
        messages = [{"role": "user", "content": "Hello"}]
        with pytest.raises(RuntimeError) as exc_info:
            await generate_completion(messages=messages)

        assert "LLM generation failed" in str(exc_info.value)
