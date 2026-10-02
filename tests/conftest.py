"""Pytest shared test fixtures."""

from collections.abc import AsyncGenerator
from pathlib import Path

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
def temporal_logging_enabled() -> None:
    """Run every test with the Temporal SDK's loggers turned up to INFO.

    They default to WARNING, which hides activity and workflow lines — and
    also hides the ``TypeError`` that a keyword argument raises once the level
    is raised for real, so leaving them off would let a broken call site pass.
    """
    configure_temporal_logging()
