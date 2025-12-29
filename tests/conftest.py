"""Pytest fixtures for event bus tests."""

import tempfile
from pathlib import Path

import pytest

from event_bus.storage import SQLiteStorage


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    yield db_path
    # Cleanup
    Path(db_path).unlink(missing_ok=True)


@pytest.fixture
def storage(temp_db):
    """Create a storage instance with a temporary database."""
    return SQLiteStorage(db_path=temp_db)
