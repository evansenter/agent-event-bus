"""Tests for SQLite storage backend."""

from datetime import datetime, timedelta

import pytest

from event_bus.storage import Session, Event, SQLiteStorage, SESSION_TIMEOUT, MAX_EVENTS


class TestSessionOperations:
    """Tests for session CRUD operations."""

    def test_add_and_get_session(self, storage):
        """Test adding and retrieving a session."""
        now = datetime.now()
        session = Session(
            id="test-123",
            name="test-session",
            machine="localhost",
            cwd="/home/user/project",
            repo="project",
            registered_at=now,
            last_heartbeat=now,
            pid=12345,
        )
        storage.add_session(session)

        retrieved = storage.get_session("test-123")
        assert retrieved is not None
        assert retrieved.id == "test-123"
        assert retrieved.name == "test-session"
        assert retrieved.machine == "localhost"
        assert retrieved.cwd == "/home/user/project"
        assert retrieved.repo == "project"
        assert retrieved.pid == 12345

    def test_get_nonexistent_session(self, storage):
        """Test getting a session that doesn't exist."""
        assert storage.get_session("nonexistent") is None

    def test_update_session(self, storage):
        """Test updating an existing session (INSERT OR REPLACE)."""
        now = datetime.now()
        session = Session(
            id="test-123",
            name="original-name",
            machine="localhost",
            cwd="/home/user/project",
            repo="project",
            registered_at=now,
            last_heartbeat=now,
        )
        storage.add_session(session)

        # Update with same ID
        session.name = "updated-name"
        storage.add_session(session)

        retrieved = storage.get_session("test-123")
        assert retrieved.name == "updated-name"

    def test_delete_session(self, storage):
        """Test deleting a session."""
        now = datetime.now()
        session = Session(
            id="test-123",
            name="test-session",
            machine="localhost",
            cwd="/home/user/project",
            repo="project",
            registered_at=now,
            last_heartbeat=now,
        )
        storage.add_session(session)

        assert storage.delete_session("test-123") is True
        assert storage.get_session("test-123") is None

    def test_delete_nonexistent_session(self, storage):
        """Test deleting a session that doesn't exist."""
        assert storage.delete_session("nonexistent") is False

    def test_list_sessions(self, storage):
        """Test listing all sessions."""
        now = datetime.now()
        for i in range(3):
            session = Session(
                id=f"test-{i}",
                name=f"session-{i}",
                machine="localhost",
                cwd=f"/home/user/project{i}",
                repo=f"project{i}",
                registered_at=now,
                last_heartbeat=now,
            )
            storage.add_session(session)

        sessions = storage.list_sessions()
        assert len(sessions) == 3
        ids = {s.id for s in sessions}
        assert ids == {"test-0", "test-1", "test-2"}

    def test_session_count(self, storage):
        """Test counting sessions."""
        assert storage.session_count() == 0

        now = datetime.now()
        for i in range(5):
            session = Session(
                id=f"test-{i}",
                name=f"session-{i}",
                machine="localhost",
                cwd=f"/home/user/project{i}",
                repo=f"project{i}",
                registered_at=now,
                last_heartbeat=now,
            )
            storage.add_session(session)

        assert storage.session_count() == 5


class TestSessionDeduplication:
    """Tests for session deduplication by machine+cwd+pid."""

    def test_find_session_by_key(self, storage):
        """Test finding a session by machine+cwd+pid key."""
        now = datetime.now()
        session = Session(
            id="test-123",
            name="test-session",
            machine="localhost",
            cwd="/home/user/project",
            repo="project",
            registered_at=now,
            last_heartbeat=now,
            pid=12345,
        )
        storage.add_session(session)

        found = storage.find_session_by_key("localhost", "/home/user/project", 12345)
        assert found is not None
        assert found.id == "test-123"

    def test_find_session_by_key_not_found(self, storage):
        """Test finding a session that doesn't match."""
        now = datetime.now()
        session = Session(
            id="test-123",
            name="test-session",
            machine="localhost",
            cwd="/home/user/project",
            repo="project",
            registered_at=now,
            last_heartbeat=now,
            pid=12345,
        )
        storage.add_session(session)

        # Different machine
        assert storage.find_session_by_key("other-host", "/home/user/project", 12345) is None
        # Different cwd
        assert storage.find_session_by_key("localhost", "/other/path", 12345) is None
        # Different pid
        assert storage.find_session_by_key("localhost", "/home/user/project", 99999) is None


class TestHeartbeat:
    """Tests for heartbeat functionality."""

    def test_update_heartbeat(self, storage):
        """Test updating session heartbeat."""
        now = datetime.now()
        session = Session(
            id="test-123",
            name="test-session",
            machine="localhost",
            cwd="/home/user/project",
            repo="project",
            registered_at=now,
            last_heartbeat=now,
        )
        storage.add_session(session)

        new_time = now + timedelta(hours=1)
        assert storage.update_heartbeat("test-123", new_time) is True

        retrieved = storage.get_session("test-123")
        assert retrieved.last_heartbeat >= new_time

    def test_update_heartbeat_nonexistent(self, storage):
        """Test updating heartbeat for nonexistent session."""
        assert storage.update_heartbeat("nonexistent", datetime.now()) is False


class TestStaleSessionCleanup:
    """Tests for stale session cleanup."""

    def test_cleanup_stale_sessions(self, storage):
        """Test cleaning up sessions past timeout."""
        now = datetime.now()

        # Fresh session (should not be cleaned up)
        fresh = Session(
            id="fresh",
            name="fresh-session",
            machine="localhost",
            cwd="/home/user/fresh",
            repo="fresh",
            registered_at=now,
            last_heartbeat=now,
        )
        storage.add_session(fresh)

        # Stale session (should be cleaned up)
        stale_time = now - timedelta(seconds=SESSION_TIMEOUT + 100)
        stale = Session(
            id="stale",
            name="stale-session",
            machine="localhost",
            cwd="/home/user/stale",
            repo="stale",
            registered_at=stale_time,
            last_heartbeat=stale_time,
        )
        storage.add_session(stale)

        count = storage.cleanup_stale_sessions()
        assert count == 1

        assert storage.get_session("fresh") is not None
        assert storage.get_session("stale") is None

    def test_cleanup_with_custom_timeout(self, storage):
        """Test cleanup with custom timeout value."""
        now = datetime.now()

        session = Session(
            id="test",
            name="test-session",
            machine="localhost",
            cwd="/home/user/test",
            repo="test",
            registered_at=now - timedelta(seconds=60),
            last_heartbeat=now - timedelta(seconds=60),
        )
        storage.add_session(session)

        # Should not be cleaned with default timeout
        assert storage.cleanup_stale_sessions() == 0
        assert storage.get_session("test") is not None

        # Should be cleaned with 30 second timeout
        assert storage.cleanup_stale_sessions(timeout_seconds=30) == 1
        assert storage.get_session("test") is None


class TestEventOperations:
    """Tests for event CRUD operations."""

    def test_add_event(self, storage):
        """Test adding an event."""
        event = storage.add_event(
            event_type="test_event",
            payload="test payload",
            session_id="session-123",
        )

        assert event.id is not None
        assert event.event_type == "test_event"
        assert event.payload == "test payload"
        assert event.session_id == "session-123"
        assert event.channel == "all"  # default

    def test_add_event_with_channel(self, storage):
        """Test adding an event with specific channel."""
        event = storage.add_event(
            event_type="direct_message",
            payload="hello",
            session_id="sender-123",
            channel="session:receiver-456",
        )

        assert event.channel == "session:receiver-456"

    def test_get_events(self, storage):
        """Test retrieving events."""
        # Add some events
        for i in range(5):
            storage.add_event(
                event_type=f"event_{i}",
                payload=f"payload {i}",
                session_id="session-123",
            )

        events = storage.get_events()
        assert len(events) == 5

    def test_get_events_since_id(self, storage):
        """Test retrieving events since a given ID."""
        event_ids = []
        for i in range(5):
            event = storage.add_event(
                event_type=f"event_{i}",
                payload=f"payload {i}",
                session_id="session-123",
            )
            event_ids.append(event.id)

        # Get events after the third one
        events = storage.get_events(since_id=event_ids[2])
        assert len(events) == 2
        assert events[0].event_type == "event_3"
        assert events[1].event_type == "event_4"

    def test_get_events_with_limit(self, storage):
        """Test retrieving events with a limit."""
        for i in range(10):
            storage.add_event(
                event_type=f"event_{i}",
                payload=f"payload {i}",
                session_id="session-123",
            )

        events = storage.get_events(limit=3)
        assert len(events) == 3

    def test_get_last_event_id(self, storage):
        """Test getting the last event ID."""
        assert storage.get_last_event_id() == 0

        for i in range(3):
            event = storage.add_event(
                event_type=f"event_{i}",
                payload=f"payload {i}",
                session_id="session-123",
            )

        assert storage.get_last_event_id() == event.id


class TestEventChannelFiltering:
    """Tests for event channel filtering."""

    def test_get_events_by_channels(self, storage):
        """Test filtering events by channel list."""
        # Add events to different channels
        storage.add_event("broadcast", "msg1", "s1", channel="all")
        storage.add_event("direct", "msg2", "s1", channel="session:abc")
        storage.add_event("repo", "msg3", "s1", channel="repo:myrepo")
        storage.add_event("machine", "msg4", "s1", channel="machine:localhost")
        storage.add_event("other", "msg5", "s1", channel="session:xyz")

        # Filter for specific channels
        events = storage.get_events(channels=["all", "session:abc", "repo:myrepo"])
        assert len(events) == 3
        types = {e.event_type for e in events}
        assert types == {"broadcast", "direct", "repo"}

    def test_get_events_no_channel_filter(self, storage):
        """Test getting all events when no channel filter is provided."""
        storage.add_event("e1", "msg1", "s1", channel="all")
        storage.add_event("e2", "msg2", "s1", channel="session:abc")
        storage.add_event("e3", "msg3", "s1", channel="repo:myrepo")

        # No channel filter = all events
        events = storage.get_events(channels=None)
        assert len(events) == 3


class TestEventCleanup:
    """Tests for event retention/cleanup."""

    def test_event_cleanup_on_add(self, temp_db):
        """Test that old events are cleaned up when MAX_EVENTS is exceeded."""
        # Create storage with a lower MAX_EVENTS for testing
        import event_bus.storage as storage_module
        original_max = storage_module.MAX_EVENTS
        storage_module.MAX_EVENTS = 10

        try:
            storage = SQLiteStorage(db_path=temp_db)

            # Add more events than MAX_EVENTS
            for i in range(15):
                storage.add_event(
                    event_type=f"event_{i}",
                    payload=f"payload {i}",
                    session_id="session-123",
                )

            # Should only have MAX_EVENTS (10) events
            events = storage.get_events(limit=100)
            assert len(events) == 10

            # Should have the most recent events
            types = [e.event_type for e in events]
            assert "event_5" in types  # First retained event
            assert "event_14" in types  # Last event
            assert "event_0" not in types  # Should be cleaned up

        finally:
            storage_module.MAX_EVENTS = original_max


class TestDatabaseInitialization:
    """Tests for database initialization."""

    def test_creates_directory_if_needed(self, tmp_path):
        """Test that storage creates parent directories."""
        db_path = tmp_path / "subdir" / "nested" / "test.db"
        storage = SQLiteStorage(db_path=str(db_path))

        assert db_path.exists()
        # Verify it works
        assert storage.session_count() == 0

    def test_schema_migration_pid_column(self, temp_db):
        """Test that pid column is added to existing schema."""
        # This is implicitly tested by using the storage,
        # but we verify the column exists
        storage = SQLiteStorage(db_path=temp_db)

        now = datetime.now()
        session = Session(
            id="test",
            name="test",
            machine="localhost",
            cwd="/test",
            repo="test",
            registered_at=now,
            last_heartbeat=now,
            pid=12345,
        )
        storage.add_session(session)

        retrieved = storage.get_session("test")
        assert retrieved.pid == 12345

    def test_schema_migration_channel_column(self, temp_db):
        """Test that channel column is added to existing schema."""
        storage = SQLiteStorage(db_path=temp_db)

        event = storage.add_event(
            event_type="test",
            payload="test",
            session_id="s1",
            channel="repo:myrepo",
        )

        events = storage.get_events()
        assert len(events) == 1
        assert events[0].channel == "repo:myrepo"
