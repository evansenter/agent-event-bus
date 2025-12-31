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
from event_bus.helpers import (  # noqa: E402
    escape_applescript_string,
    extract_repo_from_cwd,
    is_client_alive,
)
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


@pytest.fixture(autouse=True)
def mock_dm_notifications(request):
    """Prevent real notifications from DM events during tests.

    DM events trigger _notify_dm_recipient() which calls send_notification(),
    sending real macOS notifications during test runs. We mock at the DM level
    so that TestNotify can still test send_notification behavior.

    Tests in TestAutoNotifyOnDM are excluded because they specifically test
    DM notification behavior (and mock send_notification directly).
    """
    # Skip mocking for tests that explicitly test DM notification behavior
    if request.node.cls and request.node.cls.__name__ == "TestAutoNotifyOnDM":
        yield None
        return

    with patch("event_bus.server._notify_dm_recipient") as mock:
        yield mock


class TestExtractRepoFromCwd:
    """Tests for extract_repo_from_cwd helper."""

    def test_simple_path(self):
        """Test extracting repo from simple path."""
        assert extract_repo_from_cwd("/home/user/myproject") == "myproject"

    def test_trailing_slash(self):
        """Test path with trailing slash."""
        assert extract_repo_from_cwd("/home/user/myproject/") == "myproject"

    def test_worktree_path(self):
        """Test extracting repo from worktree path."""
        assert (
            extract_repo_from_cwd("/home/user/myproject/.worktrees/feature-branch") == "myproject"
        )

    def test_empty_path(self):
        """Test empty path."""
        assert extract_repo_from_cwd("") == "unknown"

    def test_sanitizes_special_chars(self):
        """Test that special characters in path are sanitized."""
        # Newlines, tabs, carriage returns should become spaces
        assert extract_repo_from_cwd("/home/user/my\nproject") == "my project"
        assert extract_repo_from_cwd("/home/user/my\tproject") == "my project"
        assert extract_repo_from_cwd("/home/user/my\rproject") == "my project"


class TestEscapeApplescriptString:
    """Tests for escape_applescript_string security helper."""

    def test_backslash_escaping(self):
        """Test backslash characters are escaped."""
        assert escape_applescript_string("path\\to\\file") == "path\\\\to\\\\file"

    def test_quote_escaping(self):
        """Test double quote characters are escaped."""
        assert escape_applescript_string('say "hello"') == 'say \\"hello\\"'

    def test_combined_escaping_order(self):
        """Test backslashes are escaped before quotes (order matters)."""
        # Input: test\"  (backslash then quote)
        # After backslash escape: test\\"
        # After quote escape: test\\"
        assert escape_applescript_string('test\\"') == 'test\\\\\\"'

    def test_injection_attempt(self):
        """Test that command injection attempts are neutralized."""
        # Classic AppleScript injection: break out of string and execute shell
        malicious = '"; do shell script "rm -rf /"'
        escaped = escape_applescript_string(malicious)
        # Quotes should be escaped, preventing breakout
        assert '\\"' in escaped
        assert '" do shell script "' not in escaped

    def test_empty_string(self):
        """Test empty string passes through."""
        assert escape_applescript_string("") == ""

    def test_normal_text(self):
        """Test normal text without special chars passes through."""
        assert escape_applescript_string("Hello World") == "Hello World"

    def test_unicode_text(self):
        """Test Unicode characters pass through unchanged."""
        assert escape_applescript_string("Hello 世界 🎉") == "Hello 世界 🎉"


class TestSessionGetProjectName:
    """Tests for Session.get_project_name helper."""

    def test_get_project_name_with_repo(self):
        """Test that repo field takes precedence."""
        session = Session(
            id="test",
            name="test",
            machine="m",
            cwd="/home/user/myproject",
            repo="my-repo",
            registered_at=datetime.now(),
            last_heartbeat=datetime.now(),
        )
        assert session.get_project_name() == "my-repo"

    def test_get_project_name_fallback_to_cwd(self):
        """Test fallback to cwd basename when repo is empty."""
        session = Session(
            id="test",
            name="test",
            machine="m",
            cwd="/home/user/fallback-proj",
            repo="",
            registered_at=datetime.now(),
            last_heartbeat=datetime.now(),
        )
        assert session.get_project_name() == "fallback-proj"

    def test_get_project_name_none_cwd(self):
        """Test fallback to 'unknown' when cwd is None."""
        session = Session(
            id="test",
            name="test",
            machine="m",
            cwd=None,
            repo="",
            registered_at=datetime.now(),
            last_heartbeat=datetime.now(),
        )
        assert session.get_project_name() == "unknown"

    def test_get_project_name_root_directory(self):
        """Test that root directory returns 'unknown' not empty string."""
        session = Session(
            id="test",
            name="test",
            machine="m",
            cwd="/",
            repo="",
            registered_at=datetime.now(),
            last_heartbeat=datetime.now(),
        )
        assert session.get_project_name() == "unknown"

    def test_get_project_name_trailing_slash(self):
        """Test that trailing slashes are handled correctly."""
        session = Session(
            id="test",
            name="test",
            machine="m",
            cwd="/home/user/myproject/",
            repo="",
            registered_at=datetime.now(),
            last_heartbeat=datetime.now(),
        )
        assert session.get_project_name() == "myproject"

    def test_get_project_name_returns_repo_directly(self):
        """Test that repo field is returned directly without re-sanitization.

        Uses a value with special chars to prove repo is returned as-is.
        In practice, repo is sanitized at write time by extract_repo_from_cwd().
        """
        session = Session(
            id="test",
            name="test",
            machine="m",
            cwd="/home/user/project",
            repo="my\nrepo",  # Special char to prove no re-sanitization
            registered_at=datetime.now(),
            last_heartbeat=datetime.now(),
        )
        # Returns as-is (not "my repo") - sanitization happened at write time
        assert session.get_project_name() == "my\nrepo"

    def test_get_project_name_fallback_sanitizes(self):
        """Test that fallback path sanitizes special characters (defense-in-depth)."""
        session = Session(
            id="test",
            name="test",
            machine="m",
            cwd="/home/user/project\nwith\tnewlines",
            repo="",  # Empty repo forces fallback to cwd
            registered_at=datetime.now(),
            last_heartbeat=datetime.now(),
        )
        # Newlines and tabs should be replaced with spaces
        assert session.get_project_name() == "project with newlines"


class TestIsClientAlive:
    """Tests for is_client_alive helper."""

    def test_current_process_is_alive(self):
        """Test that current process PID is detected as alive on local machine."""
        assert is_client_alive(str(os.getpid()), is_local=True) is True

    def test_none_client_id(self):
        """Test that None client_id is treated as alive."""
        assert is_client_alive(None, is_local=True) is True

    def test_nonexistent_pid(self):
        """Test that nonexistent PID is detected as dead on local machine."""
        # Use a very high PID that's unlikely to exist
        assert is_client_alive("999999999", is_local=True) is False

    def test_remote_session_always_alive(self):
        """Test that remote sessions are always considered alive."""
        # Even with dead PID, remote sessions can't be checked
        assert is_client_alive("999999999", is_local=False) is True

    def test_non_numeric_client_id_treated_as_alive(self):
        """Test that non-numeric client_id is treated as alive."""
        # Can't check liveness of non-PID client IDs
        assert is_client_alive("abc-session-id", is_local=True) is True

    def test_empty_string_client_id_treated_as_alive(self):
        """Test that empty string client_id is treated as alive."""
        # Empty string can't be parsed as PID, so treated as alive
        assert is_client_alive("", is_local=True) is True


class TestRegisterSession:
    """Tests for register_session tool."""

    def test_register_new_session(self):
        """Test registering a new session."""
        result = register_session(
            name="test-session",
            machine="test-machine",
            cwd="/home/user/project",
            client_id="12345",
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
        """Test resuming an existing session with same machine+client_id."""
        # Register first session
        result1 = register_session(
            name="original-name",
            machine="test-machine",
            cwd="/home/user/project",
            client_id="12345",
        )
        session_id = result1["session_id"]

        # Register again with same key but different name (and different cwd)
        result2 = register_session(
            name="new-name",
            machine="test-machine",
            cwd="/home/user/other-project",  # cwd is no longer part of dedup key
            client_id="12345",
        )

        assert result2["session_id"] == session_id
        assert result2["name"] == "new-name"
        assert result2["resumed"] is True

    def test_new_session_different_client_id(self):
        """Test that different client_id creates new session."""
        result1 = register_session(
            name="session1",
            machine="test-machine",
            cwd="/home/user/project",
            client_id="12345",
        )

        result2 = register_session(
            name="session2",
            machine="test-machine",
            cwd="/home/user/project",
            client_id="67890",
        )

        assert result1["session_id"] != result2["session_id"]
        assert result2["active_sessions"] == 2

    def test_no_deduplication_without_client_id(self):
        """Test that sessions without client_id are never deduplicated."""
        result1 = register_session(
            name="session1",
            machine="test-machine",
            cwd="/home/user/project",
            client_id=None,
        )

        result2 = register_session(
            name="session2",
            machine="test-machine",  # Same machine
            cwd="/home/user/project",  # Same cwd
            client_id=None,  # Both None
        )

        # Should create two separate sessions (no deduplication without client_id)
        assert result1["session_id"] != result2["session_id"]
        assert result2["resumed"] is False


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

    def test_list_sessions_includes_client_id(self):
        """Test that listed sessions include client_id."""
        # Use a remote machine name so liveness check is skipped
        register_session(name="session1", machine="remote-host", client_id="abc123")

        result = list_sessions()
        assert len(result) == 1
        assert result[0]["client_id"] == "abc123"

    def test_list_sessions_cleans_dead_local_clients(self):
        """Test that dead local clients (by PID) are cleaned up."""
        # Register a session with a dead PID as client_id
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
            client_id="999999999",  # Nonexistent PID as string
        )
        server.storage.add_session(session)

        # List should not include the dead session
        result = list_sessions()
        assert len(result) == 0

        # Session should be deleted
        assert server.storage.get_session("dead-session") is None

    def test_list_sessions_ordered_by_most_recent_activity(self):
        """Test that sessions are returned most recently active first."""
        import time

        # Register sessions with delays to ensure different heartbeat times
        register_session(name="oldest", machine="remote-1", cwd="/path1")
        time.sleep(0.01)
        register_session(name="middle", machine="remote-2", cwd="/path2")
        time.sleep(0.01)
        register_session(name="newest", machine="remote-3", cwd="/path3")

        result = list_sessions()

        # Should be ordered: newest, middle, oldest (most recent first)
        names = [s["name"] for s in result]
        assert names == ["newest", "middle", "oldest"]

    def test_list_sessions_ordering_reflects_heartbeat_updates(self):
        """Test that ordering updates when heartbeat is refreshed."""
        import time

        # Register sessions
        reg1 = register_session(name="first", machine="remote-1", cwd="/path1")
        time.sleep(0.01)
        register_session(name="second", machine="remote-2", cwd="/path2")

        # Initially, second should be first (most recent)
        result = list_sessions()
        assert result[0]["name"] == "second"

        # Refresh first session's heartbeat via get_events
        time.sleep(0.01)
        get_events(session_id=reg1["session_id"])

        # Now first should be first (most recent heartbeat)
        result = list_sessions()
        assert result[0]["name"] == "first"


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
        reg_result = register_session(name="test", client_id=str(os.getpid()))
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


class TestGetEventsOrdering:
    """Tests for get_events ordering behavior."""

    def test_since_id_zero_returns_newest_first(self):
        """Test that since_id=0 returns events newest first (DESC)."""
        # Clear storage and publish events in order
        server.storage = SQLiteStorage(db_path=os.environ["EVENT_BUS_DB"])

        publish_event("first", "1")
        publish_event("second", "2")
        publish_event("third", "3")

        # Get events with since_id=0 (default)
        events = get_events(since_id=0)

        # Should be newest first: third, second, first
        types = [e["event_type"] for e in events]
        assert types.index("third") < types.index("second")
        assert types.index("second") < types.index("first")

    def test_since_id_positive_returns_chronological(self):
        """Test that since_id>0 returns events in chronological order (ASC)."""
        # Clear storage
        server.storage = SQLiteStorage(db_path=os.environ["EVENT_BUS_DB"])

        # Publish first event to get a starting point
        result1 = publish_event("first", "1")
        start_id = result1["event_id"]

        # Publish more events
        publish_event("second", "2")
        publish_event("third", "3")

        # Get events since start_id
        events = get_events(since_id=start_id)

        # Should be chronological: second, third (first is excluded by since_id)
        types = [e["event_type"] for e in events]
        assert "first" not in types  # Excluded by since_id
        assert types.index("second") < types.index("third")

    def test_polling_pattern_works(self):
        """Test the recommended polling pattern works correctly."""
        # Clear storage
        server.storage = SQLiteStorage(db_path=os.environ["EVENT_BUS_DB"])

        # Publish initial events
        publish_event("before_registration", "0")

        # Register session (simulates session start)
        reg = register_session(name="test", machine="test", cwd="/test")
        last_seen = reg["last_event_id"]

        # Publish new events after registration
        publish_event("after_registration_1", "1")
        publish_event("after_registration_2", "2")

        # Poll for new events using last_event_id from registration
        events = get_events(since_id=last_seen)

        # Should only get events AFTER registration, in chronological order
        types = [e["event_type"] for e in events]
        assert "before_registration" not in types
        assert "after_registration_1" in types
        assert "after_registration_2" in types
        # Chronological order
        assert types.index("after_registration_1") < types.index("after_registration_2")

    def test_since_id_future_returns_empty(self):
        """Test that since_id beyond current events returns empty list."""
        # Clear storage
        server.storage = SQLiteStorage(db_path=os.environ["EVENT_BUS_DB"])

        # Publish some events
        result = publish_event("test", "payload")
        last_id = result["event_id"]

        # Query with a future ID that doesn't exist yet
        events = get_events(since_id=last_id + 1000)

        # Should return empty list
        assert events == []


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

    @patch("event_bus.helpers.platform.system")
    @patch("event_bus.helpers.shutil.which")
    @patch("event_bus.helpers.subprocess.run")
    def test_notify_macos_terminal_notifier(self, mock_run, mock_which, mock_system):
        """Test notification on macOS with terminal-notifier."""
        mock_system.return_value = "Darwin"
        mock_which.return_value = "/opt/homebrew/bin/terminal-notifier"
        mock_run.return_value = MagicMock()

        result = notify(title="Test", message="Hello")

        assert result["success"] is True
        assert result["title"] == "Test"
        assert result["message"] == "Hello"
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "terminal-notifier"
        assert "-group" in call_args
        assert "-sender" in call_args
        assert "com.apple.Terminal" in call_args

    @patch("event_bus.helpers.platform.system")
    @patch("event_bus.helpers.shutil.which")
    @patch("event_bus.helpers.subprocess.run")
    def test_notify_macos_terminal_notifier_with_sound(self, mock_run, mock_which, mock_system):
        """Test notification with sound on macOS using terminal-notifier."""
        mock_system.return_value = "Darwin"
        mock_which.return_value = "/opt/homebrew/bin/terminal-notifier"
        mock_run.return_value = MagicMock()

        notify(title="Test", message="Hello", sound=True)

        call_args = mock_run.call_args[0][0]
        assert "-sound" in call_args
        assert "default" in call_args

    @patch.dict(os.environ, {"EVENT_BUS_ICON": "/tmp/test-icon.png"})
    @patch("event_bus.helpers.os.path.exists")
    @patch("event_bus.helpers.platform.system")
    @patch("event_bus.helpers.shutil.which")
    @patch("event_bus.helpers.subprocess.run")
    def test_notify_macos_with_custom_icon(self, mock_run, mock_which, mock_system, mock_exists):
        """Test notification with custom icon."""
        mock_system.return_value = "Darwin"
        mock_which.return_value = "/opt/homebrew/bin/terminal-notifier"
        mock_exists.return_value = True
        mock_run.return_value = MagicMock()

        notify(title="Test", message="Hello")

        call_args = mock_run.call_args[0][0]
        assert "-appIcon" in call_args
        assert "/tmp/test-icon.png" in call_args

    @patch("event_bus.helpers.platform.system")
    @patch("event_bus.helpers.shutil.which")
    @patch("event_bus.helpers.subprocess.run")
    def test_notify_macos_osascript_fallback(self, mock_run, mock_which, mock_system):
        """Test notification falls back to osascript when terminal-notifier not available."""
        mock_system.return_value = "Darwin"
        mock_which.return_value = None  # No terminal-notifier
        mock_run.return_value = MagicMock()

        result = notify(title="Test", message="Hello")

        assert result["success"] is True
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "osascript"

    @patch("event_bus.helpers.platform.system")
    @patch("event_bus.helpers.shutil.which")
    @patch("event_bus.helpers.subprocess.run")
    def test_notify_macos_osascript_with_sound(self, mock_run, mock_which, mock_system):
        """Test notification with sound using osascript fallback."""
        mock_system.return_value = "Darwin"
        mock_which.return_value = None  # No terminal-notifier
        mock_run.return_value = MagicMock()

        notify(title="Test", message="Hello", sound=True)

        call_args = mock_run.call_args[0][0]
        assert "sound name" in call_args[2]

    @patch("event_bus.helpers.platform.system")
    @patch("event_bus.helpers.shutil.which")
    @patch("event_bus.helpers.subprocess.run")
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

    @patch("event_bus.helpers.platform.system")
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
        reg = register_session(name="test", client_id=str(os.getpid()))
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


class TestRegisterSessionTip:
    """Tests for tip field in register_session response."""

    def test_new_session_includes_tip(self):
        """Test that new session registration includes a tip."""
        result = register_session(name="test-session", machine="test-machine", cwd="/test")

        assert "tip" in result
        assert result["session_id"] in result["tip"]
        assert "test-session" in result["tip"]
        assert "get_events(" in result["tip"]

    def test_resumed_session_includes_tip(self):
        """Test that resumed session includes a tip."""
        register_session(name="original", machine="test", cwd="/test", client_id="12345")
        result = register_session(name="resumed", machine="test", cwd="/test", client_id="12345")

        assert "tip" in result
        assert result["session_id"] in result["tip"]


class TestRegisterSessionLastEventId:
    """Tests for last_event_id in register_session response."""

    def test_new_session_includes_last_event_id(self):
        """Test that new session registration includes last_event_id."""
        result = register_session(name="test-session", machine="test-machine", cwd="/test")

        assert "last_event_id" in result
        assert isinstance(result["last_event_id"], int)

    def test_resumed_session_includes_last_event_id(self):
        """Test that resumed session includes last_event_id."""
        register_session(name="original", machine="test", cwd="/test", client_id="12345")
        result = register_session(name="resumed", machine="test", cwd="/test", client_id="12345")

        assert "last_event_id" in result
        assert isinstance(result["last_event_id"], int)

    def test_last_event_id_reflects_current_state(self):
        """Test that last_event_id reflects current event state."""
        # Publish some events before registration
        publish_event("pre_event1", "payload1")
        result1 = publish_event("pre_event2", "payload2")
        before_registration_id = result1["event_id"]

        # Register session - note that registration itself publishes a session_registered event
        reg_result = register_session(name="test", machine="test", cwd="/test")

        # last_event_id should be >= the pre-registration event (registration adds one more event)
        assert reg_result["last_event_id"] > before_registration_id
        # And specifically, it should be exactly 1 more (the session_registered event)
        assert reg_result["last_event_id"] == before_registration_id + 1

    def test_last_event_id_in_tip(self):
        """Test that tip mentions last_event_id for polling."""
        result = register_session(name="test", machine="test", cwd="/test")

        assert "last_event_id" in result["tip"]
        assert "since_id" in result["tip"]


class TestAutoNotifyOnDM:
    """Tests for auto-notify on direct messages."""

    @patch("event_bus.server.send_notification")
    def test_dm_triggers_notification(self, mock_notify):
        """Test that DM to a session triggers notification."""
        # Register target session
        target = register_session(name="target-session", machine="test", cwd="/test")
        target_id = target["session_id"]

        # Register sender session
        sender = register_session(name="sender-session", machine="test", cwd="/test2")
        sender_id = sender["session_id"]

        # Send DM
        publish_event(
            event_type="help_needed",
            payload="Can you review my code?",
            session_id=sender_id,
            channel=f"session:{target_id}",
        )

        # Verify notification was sent with correct format
        mock_notify.assert_called_once()
        call_kwargs = mock_notify.call_args.kwargs
        assert "📨 target-session" in call_kwargs["title"]  # Title includes emoji and target name
        assert "test" in call_kwargs["title"]  # Title includes project name from cwd="/test"
        assert "sender-session" in call_kwargs["message"]  # Message includes sender name
        assert "Can you review my code?" in call_kwargs["message"]  # Message includes payload

    @patch("event_bus.server.send_notification")
    def test_dm_to_nonexistent_session_no_notification(self, mock_notify):
        """Test that DM to nonexistent session doesn't trigger notification."""
        publish_event(
            event_type="test",
            payload="test message",
            channel="session:nonexistent",
        )

        # No notification should be sent
        mock_notify.assert_not_called()

    @patch("event_bus.server.send_notification")
    def test_broadcast_no_notification(self, mock_notify):
        """Test that broadcast doesn't trigger notification."""
        publish_event(
            event_type="test",
            payload="broadcast message",
            channel="all",
        )

        # No notification should be sent
        mock_notify.assert_not_called()

    @patch("event_bus.server.send_notification")
    def test_dm_truncates_long_payload(self, mock_notify):
        """Test that DM notification truncates long payloads."""
        target = register_session(name="target", machine="test", cwd="/test")
        target_id = target["session_id"]

        long_payload = "x" * 100
        publish_event(
            event_type="test",
            payload=long_payload,
            channel=f"session:{target_id}",
        )

        mock_notify.assert_called_once()
        call_kwargs = mock_notify.call_args.kwargs
        message = call_kwargs["message"]

        # Verify truncation happened (full payload not in message)
        assert long_payload not in message
        # Verify ellipsis indicates truncation
        assert "..." in message
        # Verify some portion of payload is present
        assert "xxx" in message  # At least some x's

    @patch("event_bus.server.send_notification")
    def test_dm_notification_failure_still_publishes_event(self, mock_notify):
        """Test that notification failures don't prevent event publishing."""
        mock_notify.side_effect = Exception("Notification system error")

        target = register_session(name="target", machine="test", cwd="/test")
        target_id = target["session_id"]

        # Should not raise - event should still be published
        result = publish_event(
            event_type="test",
            payload="important message",
            channel=f"session:{target_id}",
        )

        assert "event_id" in result
        # Verify event was stored despite notification failure
        events = get_events(session_id=target_id)
        event_types = [e["event_type"] for e in events]
        assert "test" in event_types

    @patch("event_bus.server.send_notification")
    def test_dm_from_anonymous_sender(self, mock_notify):
        """Test that DM from anonymous sender shows 'anonymous' in notification."""
        target = register_session(name="target", machine="test", cwd="/test")
        target_id = target["session_id"]

        # Send DM without session_id
        publish_event(
            event_type="test",
            payload="anonymous message",
            session_id=None,  # Anonymous sender
            channel=f"session:{target_id}",
        )

        mock_notify.assert_called_once()
        call_kwargs = mock_notify.call_args.kwargs
        assert "anonymous" in call_kwargs["message"]
        assert "anonymous message" in call_kwargs["message"]

    @patch("event_bus.server.send_notification")
    def test_dm_from_deleted_sender_session(self, mock_notify):
        """Test that DM from deleted sender session shows anonymous."""
        target = register_session(name="target", machine="test", cwd="/test")
        target_id = target["session_id"]

        # Send DM with session_id that doesn't exist
        publish_event(
            event_type="test",
            payload="message from ghost",
            session_id="nonexistent-session",
            channel=f"session:{target_id}",
        )

        mock_notify.assert_called_once()
        call_kwargs = mock_notify.call_args.kwargs
        assert "anonymous" in call_kwargs["message"]

    @patch("event_bus.server.send_notification")
    def test_repo_channel_no_notification(self, mock_notify):
        """Test that repo channel doesn't trigger notification."""
        register_session(name="target", machine="test", cwd="/test/myrepo")

        publish_event(
            event_type="test",
            payload="repo message",
            channel="repo:myrepo",
        )

        mock_notify.assert_not_called()

    @patch("event_bus.server.send_notification")
    def test_machine_channel_no_notification(self, mock_notify):
        """Test that machine channel doesn't trigger notification."""
        publish_event(
            event_type="test",
            payload="machine message",
            channel="machine:test",
        )

        mock_notify.assert_not_called()

    @patch("event_bus.server.send_notification")
    def test_dm_with_empty_payload(self, mock_notify):
        """Test that DM with empty payload still notifies."""
        target = register_session(name="target", machine="test", cwd="/test")
        target_id = target["session_id"]

        publish_event(
            event_type="test",
            payload="",
            channel=f"session:{target_id}",
        )

        mock_notify.assert_called_once()
        call_kwargs = mock_notify.call_args.kwargs
        # Should still have sender info even with empty payload
        assert "From:" in call_kwargs["message"]

    @patch("event_bus.server.send_notification")
    def test_dm_with_very_long_session_name(self, mock_notify):
        """Test notification with very long session name in title."""
        very_long_name = "a" * 200
        target = register_session(name=very_long_name, machine="test", cwd="/test")
        target_id = target["session_id"]

        publish_event(
            event_type="test",
            payload="test message",
            channel=f"session:{target_id}",
        )

        mock_notify.assert_called_once()
        call_kwargs = mock_notify.call_args.kwargs
        # Title should contain the long name
        assert very_long_name in call_kwargs["title"]

    @patch("event_bus.server.send_notification")
    def test_dm_with_special_characters(self, mock_notify):
        """Test notification handles emoji and special characters."""
        target = register_session(name="target", machine="test", cwd="/test")
        target_id = target["session_id"]

        special_payload = "Hello 🎉\nMultiline\tWith\ttabs and emoji 😊"
        publish_event(
            event_type="test",
            payload=special_payload,
            channel=f"session:{target_id}",
        )

        mock_notify.assert_called_once()
        call_kwargs = mock_notify.call_args.kwargs
        # Should contain the special characters
        assert "🎉" in call_kwargs["message"] or "Hello" in call_kwargs["message"]
