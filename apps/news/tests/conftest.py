"""Pytest shared test fixtures."""

from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock

import edge_tts
import pytest
import pytest_asyncio

from src.api.db.database import run_migrations
from src.core.config import settings
from src.workers.client import configure_temporal_logging


@pytest_asyncio.fixture(autouse=True)
async def setup_test_database(tmp_path: Path) -> AsyncGenerator[None, None]:
    """Sets up a temporary SQLite database for each test run."""
    test_db = str(tmp_path / "test_news.db")
    settings.database_path = test_db
    await run_migrations(test_db)
    yield


@pytest.fixture(autouse=True)
def offline_edge_voices(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never reach Microsoft's voice catalogue from the test suite.

    ``edge_tts.list_voices()`` is the one network call the GUI and the digest
    voice resolution make without a mock around them (Phase 8 renders a real
    Edge voice dropdown). Tests that care about a specific catalogue patch
    ``src.api.services.tts._EDGE_VOICES`` themselves, which overrides this.
    """
    monkeypatch.setattr(edge_tts, "list_voices", AsyncMock(return_value=[]))


@pytest.fixture(autouse=True)
def temporal_logging_enabled() -> None:
    """Run every test with the Temporal SDK's loggers turned up to INFO.

    They default to WARNING, which hides activity and workflow lines — and
    also hides the ``TypeError`` that a keyword argument raises once the level
    is raised for real, so leaving them off would let a broken call site pass.
    """
    configure_temporal_logging()
