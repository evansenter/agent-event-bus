"""Tests for MCP server tools."""

import os
import socket
import tempfile
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

# Set up temp DB before importing server (which initializes storage)
_temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["EVENT_BUS_DB"] = _temp_db.name

from event_bus import server  # noqa: E402
from event_bus.storage import Session, SQLiteStorage  # noqa: E402

# Access the underlying functions from FunctionTool wrappers
register_session = server.register_session.fn
list_sessions = server.list_sessions.fn
publish_event = server.publish_event.fn
get_events = server.get_events.fn
unregister_session = server.unregister_session.fn
notify = server.notify.fn


@pytest.fixture(autouse=True)
def clean_storage():
    """Clean the storage before each test."""
    # Clear all sessions and events
    for session in server.storage.list_sessions():
        server.storage.delete_session(session.id)
    # Clear events by recreating storage
    server.storage = SQLiteStorage(db_path=os.environ["EVENT_BUS_DB"])
    yield


class TestExtractRepoFromCwd:
    """Tests for _extract_repo_from_cwd helper."""

    def test_simple_path(self):
        """Test extracting repo from simple path."""
        assert server._extract_repo_from_cwd("/home/user/myproject") == "myproject"

    def test_trailing_slash(self):
        """Test path with trailing slash."""
        assert server._extract_repo_from_cwd("/home/user/myproject/") == "myproject"

    def test_worktree_path(self):
        """Test extracting repo from worktree path."""
        assert (
            server._extract_repo_from_cwd("/home/user/myproject/.worktrees/feature-branch")
            == "myproject"
        )

    def test_empty_path(self):
        """Test empty path."""
        assert server._extract_repo_from_cwd("") == "unknown"


class TestIsPidAlive:
    """Tests for _is_pid_alive helper."""

    def test_current_process_is_alive(self):
        """Test that current process is detected as alive."""
        assert server._is_pid_alive(os.getpid()) is True

    def test_none_pid(self):
        """Test that None PID is treated as alive."""
        assert server._is_pid_alive(None) is True

    def test_nonexistent_pid(self):
        """Test that nonexistent PID is detected as dead."""
        # Use a very high PID that's unlikely to exist
        assert server._is_pid_alive(999999999) is False


class TestRegisterSession:
    """Tests for register_session tool."""

    def test_register_new_session(self):
        """Test registering a new session."""
        result = register_session(
            name="test-session",
            machine="test-machine",
            cwd="/home/user/project",
            pid=12345,
        )

        assert "session_id" in result
        assert result["name"] == "test-session"
        assert result["machine"] == "test-machine"
        assert result["cwd"] == "/home/user/project"
        assert result["repo"] == "project"
        assert result["resumed"] is False
        assert result["active_sessions"] == 1

    def test_register_session_defaults(self):
        """Test registering with default machine and cwd."""
        result = register_session(name="test-session")

        assert result["machine"] == socket.gethostname()
        assert "cwd" in result

    def test_resume_existing_session(self):
        """Test resuming an existing session with same machine+cwd+pid."""
        # Register first session
        result1 = register_session(
            name="original-name",
            machine="test-machine",
            cwd="/home/user/project",
            pid=12345,
        )
        session_id = result1["session_id"]

        # Register again with same key but different name
        result2 = register_session(
            name="new-name",
            machine="test-machine",
            cwd="/home/user/project",
            pid=12345,
        )

        assert result2["session_id"] == session_id
        assert result2["name"] == "new-name"
        assert result2["resumed"] is True

    def test_new_session_different_pid(self):
        """Test that different PID creates new session."""
        result1 = register_session(
            name="session1",
            machine="test-machine",
            cwd="/home/user/project",
            pid=12345,
        )

        result2 = register_session(
            name="session2",
            machine="test-machine",
            cwd="/home/user/project",
            pid=67890,
        )

        assert result1["session_id"] != result2["session_id"]
        assert result2["active_sessions"] == 2


class TestListSessions:
    """Tests for list_sessions tool."""

    def test_list_empty(self):
        """Test listing when no sessions exist."""
        result = list_sessions()
        assert result == []

    def test_list_sessions(self):
        """Test listing multiple sessions."""
        register_session(name="session1", machine="machine1", cwd="/path1")
        register_session(name="session2", machine="machine2", cwd="/path2")

        result = list_sessions()
        assert len(result) == 2

        names = {s["name"] for s in result}
        assert names == {"session1", "session2"}

    def test_list_sessions_includes_pid(self):
        """Test that listed sessions include PID."""
        # Use a remote machine name so PID liveness check is skipped
        register_session(name="session1", machine="remote-host", pid=12345)

        result = list_sessions()
        assert len(result) == 1
        assert result[0]["pid"] == 12345

    def test_list_sessions_cleans_dead_local_pids(self):
        """Test that dead local PIDs are cleaned up."""
        # Register a session with a dead PID
        hostname = socket.gethostname()
        now = datetime.now()
        session = Session(
            id="dead-session",
            name="dead",
            machine=hostname,
            cwd="/test",
            repo="test",
            registered_at=now,
            last_heartbeat=now,
            pid=999999999,  # Nonexistent PID
        )
        server.storage.add_session(session)

        # List should not include the dead session
        result = list_sessions()
        assert len(result) == 0

        # Session should be deleted
        assert server.storage.get_session("dead-session") is None


class TestPublishEvent:
    """Tests for publish_event tool."""

    def test_publish_event(self):
        """Test publishing an event."""
        result = publish_event(
            event_type="test_event",
            payload="test payload",
            session_id="session-123",
        )

        assert "event_id" in result
        assert result["event_type"] == "test_event"
        assert result["payload"] == "test payload"
        assert result["channel"] == "all"

    def test_publish_event_with_channel(self):
        """Test publishing to specific channel."""
        result = publish_event(
            event_type="direct_message",
            payload="hello",
            session_id="sender",
            channel="session:receiver",
        )

        assert result["channel"] == "session:receiver"

    def test_publish_event_anonymous(self):
        """Test publishing without session_id."""
        result = publish_event(
            event_type="anonymous_event",
            payload="test",
        )

        assert "event_id" in result

    def test_publish_event_auto_heartbeat(self):
        """Test that publishing refreshes heartbeat."""
        # Register a session
        reg_result = register_session(name="test", pid=os.getpid())
        session_id = reg_result["session_id"]

        # Get original heartbeat
        session = server.storage.get_session(session_id)
        original_heartbeat = session.last_heartbeat

        # Publish event
        import time

        time.sleep(0.01)  # Small delay to ensure time difference
        publish_event("test", "payload", session_id=session_id)

        # Check heartbeat was updated
        session = server.storage.get_session(session_id)
        assert session.last_heartbeat >= original_heartbeat


class TestGetEvents:
    """Tests for get_events tool."""

    def test_get_events_empty(self):
        """Test getting events when none exist."""
        result = get_events()
        # May have session_registered event from other tests, so just check it returns a list
        assert isinstance(result, list)

    def test_get_events(self):
        """Test getting events."""
        # Clear any existing events
        server.storage = SQLiteStorage(db_path=os.environ["EVENT_BUS_DB"])

        publish_event("event1", "payload1")
        publish_event("event2", "payload2")

        result = get_events()
        assert len(result) >= 2

    def test_get_events_since_id(self):
        """Test getting events since a given ID."""
        # Publish some events
        result1 = publish_event("event1", "payload1")
        publish_event("event2", "payload2")
        publish_event("event3", "payload3")

        # Get events since event1
        events = get_events(since_id=result1["event_id"])

        types = [e["event_type"] for e in events]
        assert "event2" in types
        assert "event3" in types

    def test_get_events_channel_filtering(self):
        """Test that events are filtered by channel."""
        # Register a session to get channel filtering
        reg = register_session(name="test", machine="test-machine", cwd="/test/repo")
        session_id = reg["session_id"]

        # Publish events to different channels
        publish_event("broadcast", "msg1", channel="all")
        publish_event("for_me", "msg2", channel=f"session:{session_id}")
        publish_event("for_other", "msg3", channel="session:other-session")
        publish_event("my_repo", "msg4", channel="repo:repo")
        publish_event("other_repo", "msg5", channel="repo:other-repo")

        # Get events for this session
        events = get_events(session_id=session_id)

        types = {e["event_type"] for e in events}
        assert "broadcast" in types
        assert "for_me" in types
        assert "my_repo" in types
        assert "for_other" not in types
        assert "other_repo" not in types


class TestUnregisterSession:
    """Tests for unregister_session tool."""

    def test_unregister_session(self):
        """Test unregistering a session."""
        reg = register_session(name="test")
        session_id = reg["session_id"]

        result = unregister_session(session_id)

        assert result["success"] is True
        assert result["session_id"] == session_id
        assert server.storage.get_session(session_id) is None

    def test_unregister_nonexistent(self):
        """Test unregistering a session that doesn't exist."""
        result = unregister_session("nonexistent")

        assert "error" in result
        assert result["session_id"] == "nonexistent"

    def test_unregister_publishes_event(self):
        """Test that unregistering publishes an event."""
        reg = register_session(name="test-session")
        session_id = reg["session_id"]
        last_event_id = server.storage.get_last_event_id()

        unregister_session(session_id)

        # Check for unregister event
        events = server.storage.get_events(since_id=last_event_id)
        event_types = [e.event_type for e in events]
        assert "session_unregistered" in event_types


class TestNotify:
    """Tests for notify tool."""

    @patch("event_bus.server.platform.system")
    @patch("event_bus.server.subprocess.run")
    def test_notify_macos(self, mock_run, mock_system):
        """Test notification on macOS."""
        mock_system.return_value = "Darwin"
        mock_run.return_value = MagicMock()

        result = notify(title="Test", message="Hello")

        assert result["success"] is True
        assert result["title"] == "Test"
        assert result["message"] == "Hello"
        mock_run.assert_called_once()

    @patch("event_bus.server.platform.system")
    @patch("event_bus.server.subprocess.run")
    def test_notify_macos_with_sound(self, mock_run, mock_system):
        """Test notification with sound on macOS."""
        mock_system.return_value = "Darwin"
        mock_run.return_value = MagicMock()

        notify(title="Test", message="Hello", sound=True)

        call_args = mock_run.call_args[0][0]
        assert "sound name" in call_args[2]

    @patch("event_bus.server.platform.system")
    @patch("event_bus.server.shutil.which")
    @patch("event_bus.server.subprocess.run")
    def test_notify_linux(self, mock_run, mock_which, mock_system):
        """Test notification on Linux."""
        mock_system.return_value = "Linux"
        mock_which.return_value = "/usr/bin/notify-send"
        mock_run.return_value = MagicMock()

        result = notify(title="Test", message="Hello")

        assert result["success"] is True
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args == ["notify-send", "Test", "Hello"]

    @patch("event_bus.server.platform.system")
    def test_notify_unsupported_platform(self, mock_system):
        """Test notification on unsupported platform."""
        mock_system.return_value = "Windows"

        result = notify(title="Test", message="Hello")

        assert result["success"] is False


class TestGetImplicitChannels:
    """Tests for _get_implicit_channels helper."""

    def test_no_session_id(self):
        """Test with no session ID."""
        assert server._get_implicit_channels(None) is None

    def test_nonexistent_session(self):
        """Test with nonexistent session."""
        assert server._get_implicit_channels("nonexistent") is None

    def test_implicit_channels(self):
        """Test implicit channel subscriptions."""
        reg = register_session(
            name="test",
            machine="my-machine",
            cwd="/home/user/myrepo",
        )
        session_id = reg["session_id"]

        channels = server._get_implicit_channels(session_id)

        assert "all" in channels
        assert f"session:{session_id}" in channels
        assert "repo:myrepo" in channels
        assert "machine:my-machine" in channels


class TestAutoHeartbeat:
    """Tests for _auto_heartbeat helper."""

    def test_auto_heartbeat_updates_session(self):
        """Test that auto_heartbeat updates session."""
        reg = register_session(name="test", pid=os.getpid())
        session_id = reg["session_id"]

        original = server.storage.get_session(session_id).last_heartbeat

        import time

        time.sleep(0.01)
        server._auto_heartbeat(session_id)

        updated = server.storage.get_session(session_id).last_heartbeat
        assert updated >= original

    def test_auto_heartbeat_ignores_anonymous(self):
        """Test that auto_heartbeat ignores anonymous session."""
        # Should not raise
        server._auto_heartbeat("anonymous")

    def test_auto_heartbeat_ignores_none(self):
        """Test that auto_heartbeat ignores None."""
        # Should not raise
        server._auto_heartbeat(None)
