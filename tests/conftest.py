"""Pytest shared test fixtures."""

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest_asyncio

from src.api.db.database import run_migrations
from src.core.config import settings


@pytest_asyncio.fixture(autouse=True)
async def setup_test_database(tmp_path: Path) -> AsyncGenerator[None, None]:
    """Sets up a temporary SQLite database for each test run."""
    test_db = str(tmp_path / "test_news.db")
    settings.database_path = test_db
    await run_migrations(test_db)
    yield
