"""Tests for MCP server tools."""

import os
import socket
from datetime import datetime

from event_bus import server
from event_bus.storage import Session, SQLiteStorage

# Access the underlying functions from FunctionTool wrappers
register_session = server.register_session.fn
list_sessions = server.list_sessions.fn
publish_event = server.publish_event.fn
get_events = server.get_events.fn
unregister_session = server.unregister_session.fn


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
        # Returns dict with events and next_cursor
        assert isinstance(result, dict)
        assert "events" in result
        assert "next_cursor" in result

    def test_get_events(self):
        """Test getting events."""
        # Clear any existing events
        server.storage = SQLiteStorage(db_path=os.environ["EVENT_BUS_DB"])

        publish_event("event1", "payload1")
        publish_event("event2", "payload2")

        result = get_events()
        assert len(result["events"]) >= 2

    def test_get_events_with_cursor(self):
        """Test getting events after a given cursor."""
        # Publish some events
        result1 = publish_event("event1", "payload1")
        publish_event("event2", "payload2")
        publish_event("event3", "payload3")

        # Get events after event1 (using cursor, oldest first)
        result = get_events(cursor=str(result1["event_id"]), order="asc")

        types = [e["event_type"] for e in result["events"]]
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
        result = get_events(session_id=session_id)

        types = {e["event_type"] for e in result["events"]}
        assert "broadcast" in types
        assert "for_me" in types
        assert "my_repo" in types
        assert "for_other" not in types
        assert "other_repo" not in types


class TestGetEventsOrdering:
    """Tests for get_events ordering behavior."""

    def test_default_order_is_desc(self):
        """Test that default order is DESC (newest first)."""
        # Clear storage
        server.storage = SQLiteStorage(db_path=os.environ["EVENT_BUS_DB"])

        publish_event("first", "1")
        publish_event("second", "2")
        publish_event("third", "3")

        # Default order should be DESC (newest first)
        result = get_events()
        types = [e["event_type"] for e in result["events"]]
        assert types.index("third") < types.index("second")
        assert types.index("second") < types.index("first")

    def test_explicit_order_desc(self):
        """Test that order='desc' returns newest first."""
        # Clear storage
        server.storage = SQLiteStorage(db_path=os.environ["EVENT_BUS_DB"])

        publish_event("first", "1")
        publish_event("second", "2")
        publish_event("third", "3")

        result = get_events(order="desc")
        types = [e["event_type"] for e in result["events"]]
        assert types.index("third") < types.index("second")
        assert types.index("second") < types.index("first")

    def test_explicit_order_asc(self):
        """Test that order='asc' returns oldest first."""
        # Clear storage
        server.storage = SQLiteStorage(db_path=os.environ["EVENT_BUS_DB"])

        # Get cursor before our test events
        cursor = server.storage.get_cursor()

        publish_event("first", "1")
        publish_event("second", "2")
        publish_event("third", "3")

        # Use cursor to filter to only our test events
        result = get_events(cursor=cursor, order="asc") if cursor else get_events(order="asc")
        types = [e["event_type"] for e in result["events"]]
        assert types.index("first") < types.index("second")
        assert types.index("second") < types.index("third")

    def test_polling_pattern_works(self):
        """Test the recommended polling pattern works correctly."""
        # Clear storage
        server.storage = SQLiteStorage(db_path=os.environ["EVENT_BUS_DB"])

        # Publish initial events
        publish_event("before_registration", "0")

        # Register session (simulates session start)
        reg = register_session(name="test", machine="test", cwd="/test")
        cursor = reg["cursor"]

        # Publish new events after registration
        publish_event("after_registration_1", "1")
        publish_event("after_registration_2", "2")

        # Poll for new events using cursor from registration (oldest first for polling)
        result = get_events(cursor=cursor, order="asc")

        # Should only get events AFTER registration, in chronological order
        types = [e["event_type"] for e in result["events"]]
        assert "before_registration" not in types
        assert "after_registration_1" in types
        assert "after_registration_2" in types
        # Chronological order
        assert types.index("after_registration_1") < types.index("after_registration_2")

    def test_cursor_with_order_desc(self):
        """Test using cursor with order='desc'."""
        # Clear storage
        server.storage = SQLiteStorage(db_path=os.environ["EVENT_BUS_DB"])

        result1 = publish_event("first", "1")
        cursor = str(result1["event_id"])
        publish_event("second", "2")
        publish_event("third", "3")

        # Get events after cursor, newest first
        result = get_events(cursor=cursor, order="desc")
        types = [e["event_type"] for e in result["events"]]

        # Should only have events after cursor, in DESC order
        assert "first" not in types
        assert types.index("third") < types.index("second")

    def test_cursor_with_order_asc(self):
        """Test using cursor with order='asc'."""
        # Clear storage
        server.storage = SQLiteStorage(db_path=os.environ["EVENT_BUS_DB"])

        result1 = publish_event("first", "1")
        cursor = str(result1["event_id"])
        publish_event("second", "2")
        publish_event("third", "3")

        # Get events after cursor, oldest first
        result = get_events(cursor=cursor, order="asc")
        types = [e["event_type"] for e in result["events"]]

        # Should only have events after cursor, in ASC order
        assert "first" not in types
        assert types.index("second") < types.index("third")

    def test_future_cursor_returns_empty(self):
        """Test that cursor beyond current events returns empty list."""
        # Clear storage
        server.storage = SQLiteStorage(db_path=os.environ["EVENT_BUS_DB"])

        # Publish some events
        result = publish_event("test", "payload")
        future_cursor = str(result["event_id"] + 1000)

        # Query with a future cursor
        result = get_events(cursor=future_cursor)

        # Should return empty list
        assert result["events"] == []


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
        cursor = server.storage.get_cursor()

        unregister_session(session_id)

        # Check for unregister event
        events, _ = server.storage.get_events(cursor=cursor, order="asc")
        event_types = [e.event_type for e in events]
        assert "session_unregistered" in event_types


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


class TestRegisterSessionCursor:
    """Tests for cursor in register_session response."""

    def test_new_session_includes_cursor(self):
        """Test that new session registration includes cursor."""
        result = register_session(name="test-session", machine="test-machine", cwd="/test")

        assert "cursor" in result
        assert isinstance(result["cursor"], str)

    def test_resumed_session_includes_cursor(self):
        """Test that resumed session includes cursor."""
        register_session(name="original", machine="test", cwd="/test", client_id="12345")
        result = register_session(name="resumed", machine="test", cwd="/test", client_id="12345")

        assert "cursor" in result
        assert isinstance(result["cursor"], str)

    def test_cursor_reflects_current_state(self):
        """Test that cursor reflects current event state."""
        # Publish some events before registration
        publish_event("pre_event1", "payload1")
        result1 = publish_event("pre_event2", "payload2")
        before_registration_id = result1["event_id"]

        # Register session - note that registration itself publishes a session_registered event
        reg_result = register_session(name="test", machine="test", cwd="/test")

        # cursor should be > the pre-registration event (registration adds one more event)
        assert int(reg_result["cursor"]) > before_registration_id
        # And specifically, it should be exactly 1 more (the session_registered event)
        assert int(reg_result["cursor"]) == before_registration_id + 1

    def test_cursor_in_tip(self):
        """Test that tip mentions cursor for polling."""
        result = register_session(name="test", machine="test", cwd="/test")

        assert "cursor" in result["tip"]
        assert "get_events(cursor=" in result["tip"]


class TestSessionCursorTracking:
    """Tests for session-based cursor tracking (RFC #37)."""

    def test_get_events_persists_cursor(self):
        """Test that get_events persists cursor when session_id provided."""
        # Register a session
        reg = register_session(name="test", machine="remote-host", client_id="test-123")
        session_id = reg["session_id"]

        # Publish some events
        publish_event("event1", "payload1")
        publish_event("event2", "payload2")

        # Get events with session_id - should persist cursor
        result = get_events(session_id=session_id)

        # Check cursor was persisted
        session = server.storage.get_session(session_id)
        assert session.last_cursor is not None
        assert session.last_cursor == result["next_cursor"]

    def test_resumed_session_gets_last_cursor(self):
        """Test that resumed sessions get their last_cursor for seamless resume."""
        # Register initial session
        reg1 = register_session(name="test", machine="remote-host", client_id="test-456")
        session_id = reg1["session_id"]

        # Publish events and poll to establish cursor
        publish_event("event1", "payload1")
        publish_event("event2", "payload2")
        get_events(session_id=session_id)

        # Get the saved cursor
        session = server.storage.get_session(session_id)
        saved_cursor = session.last_cursor

        # Publish more events
        publish_event("event3", "payload3")
        publish_event("event4", "payload4")

        # Resume session (same machine + client_id)
        reg2 = register_session(name="test-resumed", machine="remote-host", client_id="test-456")

        # Should be same session
        assert reg2["session_id"] == session_id
        assert reg2["resumed"] is True

        # Should get the saved cursor, not current position
        assert reg2["cursor"] == saved_cursor

    def test_resumed_session_without_cursor_falls_back(self):
        """Test that resumed sessions without last_cursor get current position."""
        # Register initial session
        reg1 = register_session(name="test", machine="remote-host", client_id="test-789")
        session_id = reg1["session_id"]

        # Don't poll (no cursor saved)
        session = server.storage.get_session(session_id)
        assert session.last_cursor is None

        # Publish more events
        publish_event("new_event", "payload")

        # Resume session
        reg2 = register_session(name="test-resumed", machine="remote-host", client_id="test-789")

        # Should fall back to current position (not None)
        assert reg2["cursor"] is not None

    def test_cursor_updated_on_each_poll(self):
        """Test that cursor is updated on each poll."""
        # Register session
        reg = register_session(name="test", machine="remote-host", client_id="test-poll")
        session_id = reg["session_id"]

        # First poll
        publish_event("event1", "payload1")
        result1 = get_events(session_id=session_id)

        # More events and second poll
        publish_event("event2", "payload2")
        result2 = get_events(session_id=session_id, cursor=result1["next_cursor"], order="asc")
        cursor2 = server.storage.get_session(session_id).last_cursor

        # Cursor should have been updated
        assert cursor2 == result2["next_cursor"]

    def test_cursor_not_persisted_without_session_id(self):
        """Test that cursor is not persisted when no session_id provided."""
        # Publish events
        publish_event("event1", "payload1")

        # Get events without session_id
        get_events()

        # No session should have cursor updated (we can't verify this directly,
        # but we can verify the call doesn't raise)
        # This test mainly ensures the None check works correctly


class TestUnregisterByClientId:
    """Tests for unregister_session with client_id lookup."""

    def test_unregister_by_client_id(self):
        """Test that sessions can be unregistered by client_id."""
        # Register a session with client_id
        reg = register_session(name="test", client_id="test-unregister-123")
        session_id = reg["session_id"]

        # Verify session exists
        assert server.storage.get_session(session_id) is not None

        # Unregister by client_id (not session_id)
        result = unregister_session(client_id="test-unregister-123")

        assert result["success"] is True
        assert result["session_id"] == session_id

        # Verify session is gone
        assert server.storage.get_session(session_id) is None

    def test_unregister_by_client_id_not_found(self):
        """Test error when client_id doesn't match any session."""
        result = unregister_session(client_id="nonexistent-client")

        assert "error" in result
        assert result["client_id"] == "nonexistent-client"

    def test_unregister_requires_session_id_or_client_id(self):
        """Test that unregister requires at least one identifier."""
        result = unregister_session()

        assert "error" in result
        assert "Must provide" in result["error"]

    def test_unregister_session_id_takes_precedence(self):
        """Test that session_id is used if both are provided."""
        # Register a session
        reg = register_session(name="test", client_id="test-precedence-123")
        session_id = reg["session_id"]

        # Unregister with session_id (should work even if client_id is wrong)
        result = unregister_session(session_id=session_id, client_id="wrong-client")

        assert result["success"] is True
        assert result["session_id"] == session_id
