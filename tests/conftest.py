"""Pytest fixtures for event bus tests."""

import os
import tempfile
from pathlib import Path

import pytest


def pytest_configure(config):
    """Set up temp DB before any imports happen.

    This runs before test collection, ensuring AGENT_EVENT_BUS_DB is set before
    server.py is imported (which initializes storage at module level).
    """
    temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    os.environ["AGENT_EVENT_BUS_DB"] = temp_db.name
    # Prevent test migrations from logging to production log file
    os.environ["AGENT_EVENT_BUS_TESTING"] = "1"
    # Disable Tailscale auth for tests
    os.environ["AGENT_EVENT_BUS_AUTH_DISABLED"] = "1"
    # Store path for cleanup
    config._temp_db_path = temp_db.name


def pytest_unconfigure(config):
    """Clean up temp DB after all tests."""
    if hasattr(config, "_temp_db_path"):
        Path(config._temp_db_path).unlink(missing_ok=True)


@pytest.fixture
def temp_db():
    """Create a temporary database for testing (per-test isolation)."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    yield db_path
    # Cleanup
    Path(db_path).unlink(missing_ok=True)


@pytest.fixture
def storage(temp_db):
    """Create a storage instance with a temporary database.

    Note: Import is inside fixture to avoid triggering module-level storage
    initialization before AGENT_EVENT_BUS_DB is set by pytest_configure.
    """
    from agent_event_bus.storage import SQLiteStorage

    return SQLiteStorage(db_path=temp_db)


@pytest.fixture(autouse=True)
def clean_storage():
    """Clean the storage before each test.

    Note: Imports are inside fixture to avoid triggering module-level storage
    initialization in server.py before AGENT_EVENT_BUS_DB is set by pytest_configure.
    """
    from agent_event_bus import server
    from agent_event_bus.storage import SQLiteStorage

    # Clear all sessions and events
    for session in server.storage.list_sessions():
        server.storage.delete_session(session.id)
    # Clear all webhooks
    for webhook in server.storage.list_webhooks(active_only=False):
        server.storage.delete_webhook(webhook.id)
    # Clear events by recreating storage
    server.storage = SQLiteStorage(db_path=os.environ["AGENT_EVENT_BUS_DB"])
    yield


@pytest.fixture(autouse=True)
def mock_dm_notifications(request):
    """Prevent real notifications from DM events during tests.

    DM events trigger _notify_dm_recipient() which calls send_notification(),
    sending real macOS notifications during test runs. We mock at the DM level
    so that TestNotify can still test send_notification behavior.

    Tests marked with @pytest.mark.real_dm_notifications are excluded because
    they specifically test DM notification behavior (and mock send_notification
    directly).
    """
    from unittest.mock import patch

    # Skip mocking for tests that explicitly test DM notification behavior
    if request.node.get_closest_marker("real_dm_notifications"):
        yield None
        return

    with patch("agent_event_bus.server._notify_dm_recipient") as mock:
        yield mock


def make_events_args(**overrides):
    """Create Namespace for events command with sensible defaults.

    Usage:
        args = make_events_args()  # All defaults
        args = make_events_args(json=True, limit=10)  # Override specific fields
    """
    from argparse import Namespace

    defaults = dict(
        cursor=None,
        session_id=None,
        limit=None,
        exclude=None,
        timeout=10000,
        json=False,
        url=None,
        order="desc",
        channel=None,
        resume=False,
        debug=False,
        include=None,
    )
    defaults.update(overrides)
    return Namespace(**defaults)
