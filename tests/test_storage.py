"""Tests for SQLite storage backend."""

import sqlite3
from datetime import datetime, timedelta

from event_bus.storage import SESSION_TIMEOUT, Session, SQLiteStorage


class TestSessionOperations:
    """Tests for session CRUD operations."""

    def test_add_and_get_session(self, storage):
        """Test adding and retrieving a session."""
        now = datetime.now()
        session = Session(
            id="test-123",
            display_id="test-display",
            name="test-session",
            machine="localhost",
            cwd="/home/user/project",
            repo="project",
            registered_at=now,
            last_heartbeat=now,
            client_id="12345",
        )
        storage.add_session(session)

        retrieved = storage.get_session("test-123")
        assert retrieved is not None
        assert retrieved.id == "test-123"
        assert retrieved.display_id == "test-display"
        assert retrieved.name == "test-session"
        assert retrieved.machine == "localhost"
        assert retrieved.cwd == "/home/user/project"
        assert retrieved.repo == "project"
        assert retrieved.client_id == "12345"

    def test_get_nonexistent_session(self, storage):
        """Test getting a session that doesn't exist."""
        assert storage.get_session("nonexistent") is None

    def test_update_session(self, storage):
        """Test updating an existing session (INSERT OR REPLACE)."""
        now = datetime.now()
        session = Session(
            id="test-123",
            display_id="test-display",
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
            display_id="test-display",
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
                display_id=f"display-{i}",
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
                display_id=f"display-{i}",
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
    """Tests for session deduplication by machine+client_id."""

    def test_find_session_by_client(self, storage):
        """Test finding a session by machine+client_id key."""
        now = datetime.now()
        session = Session(
            id="test-123",
            display_id="test-display",
            name="test-session",
            machine="localhost",
            cwd="/home/user/project",
            repo="project",
            registered_at=now,
            last_heartbeat=now,
            client_id="12345",
        )
        storage.add_session(session)

        found = storage.find_session_by_client("localhost", "12345")
        assert found is not None
        assert found.id == "test-123"

    def test_find_session_by_client_not_found(self, storage):
        """Test finding a session that doesn't match."""
        now = datetime.now()
        session = Session(
            id="test-123",
            display_id="test-display",
            name="test-session",
            machine="localhost",
            cwd="/home/user/project",
            repo="project",
            registered_at=now,
            last_heartbeat=now,
            client_id="12345",
        )
        storage.add_session(session)

        # Different machine
        assert storage.find_session_by_client("other-host", "12345") is None
        # Different client_id
        assert storage.find_session_by_client("localhost", "99999") is None


class TestHeartbeat:
    """Tests for heartbeat functionality."""

    def test_update_heartbeat(self, storage):
        """Test updating session heartbeat."""
        now = datetime.now()
        session = Session(
            id="test-123",
            display_id="test-display",
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
            display_id="fresh-display",
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
            display_id="stale-display",
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
            display_id="test-display",
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

        events, next_cursor = storage.get_events()
        assert len(events) == 5
        assert next_cursor is not None

    def test_get_events_with_cursor(self, storage):
        """Test retrieving events after a given cursor."""
        event_ids = []
        for i in range(5):
            event = storage.add_event(
                event_type=f"event_{i}",
                payload=f"payload {i}",
                session_id="session-123",
            )
            event_ids.append(event.id)

        # Get events after the third one (cursor is string)
        events, next_cursor = storage.get_events(cursor=str(event_ids[2]), order="asc")
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

        events, next_cursor = storage.get_events(limit=3)
        assert len(events) == 3

    def test_get_cursor(self, storage):
        """Test getting the cursor for the most recent event."""
        assert storage.get_cursor() is None

        for i in range(3):
            event = storage.add_event(
                event_type=f"event_{i}",
                payload=f"payload {i}",
                session_id="session-123",
            )

        assert storage.get_cursor() == str(event.id)

    def test_get_events_malformed_cursor(self, storage):
        """Test that malformed cursor is handled gracefully."""
        # Add some events
        for i in range(3):
            storage.add_event(
                event_type=f"event_{i}",
                payload=f"payload {i}",
                session_id="session-123",
            )

        # Malformed cursor should reset to start (return all events)
        events, _ = storage.get_events(cursor="not-a-number")
        assert len(events) == 3

        # Empty cursor works normally
        events, _ = storage.get_events(cursor="")
        assert len(events) == 3

        # Valid cursor works normally
        events, _ = storage.get_events(cursor="1", order="asc")
        assert len(events) == 2  # Events after id=1


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
        events, _ = storage.get_events(channels=["all", "session:abc", "repo:myrepo"])
        assert len(events) == 3
        types = {e.event_type for e in events}
        assert types == {"broadcast", "direct", "repo"}

    def test_get_events_no_channel_filter(self, storage):
        """Test getting all events when no channel filter is provided."""
        storage.add_event("e1", "msg1", "s1", channel="all")
        storage.add_event("e2", "msg2", "s1", channel="session:abc")
        storage.add_event("e3", "msg3", "s1", channel="repo:myrepo")

        # No channel filter = all events
        events, _ = storage.get_events(channels=None)
        assert len(events) == 3


class TestEventTypeFiltering:
    """Tests for event type filtering."""

    def test_get_events_by_event_types(self, storage):
        """Test filtering events by event_types list."""
        storage.add_event("task_completed", "finished task", "s1")
        storage.add_event("ci_completed", "CI passed", "s1")
        storage.add_event("gotcha_discovered", "found an issue", "s1")
        storage.add_event("session_registered", "new session", "s1")
        storage.add_event("task_completed", "another task", "s1")

        # Filter for specific event types
        events, _ = storage.get_events(event_types=["task_completed", "ci_completed"])
        assert len(events) == 3
        types = {e.event_type for e in events}
        assert types == {"task_completed", "ci_completed"}

    def test_get_events_single_event_type(self, storage):
        """Test filtering for a single event type."""
        storage.add_event("task_completed", "task 1", "s1")
        storage.add_event("ci_completed", "CI 1", "s1")
        storage.add_event("task_completed", "task 2", "s1")

        events, _ = storage.get_events(event_types=["gotcha_discovered"])
        assert len(events) == 0

        events, _ = storage.get_events(event_types=["task_completed"])
        assert len(events) == 2
        assert all(e.event_type == "task_completed" for e in events)

    def test_get_events_no_type_filter(self, storage):
        """Test getting all events when no event_types filter is provided."""
        storage.add_event("e1", "msg1", "s1")
        storage.add_event("e2", "msg2", "s1")
        storage.add_event("e3", "msg3", "s1")

        # No event_types filter = all events
        events, _ = storage.get_events(event_types=None)
        assert len(events) == 3

    def test_get_events_combined_filters(self, storage):
        """Test combining event_types with channel filter."""
        storage.add_event("task_completed", "task 1", "s1", channel="repo:myrepo")
        storage.add_event("ci_completed", "CI 1", "s1", channel="repo:myrepo")
        storage.add_event("task_completed", "task 2", "s1", channel="all")
        storage.add_event("gotcha_discovered", "gotcha", "s1", channel="repo:myrepo")

        # Filter by both channel and event type
        events, _ = storage.get_events(
            channels=["repo:myrepo"], event_types=["task_completed", "ci_completed"]
        )
        assert len(events) == 2
        types = {e.event_type for e in events}
        assert types == {"task_completed", "ci_completed"}


class TestDatabaseInitialization:
    """Tests for database initialization."""

    def test_creates_directory_if_needed(self, tmp_path):
        """Test that storage creates parent directories."""
        db_path = tmp_path / "subdir" / "nested" / "test.db"
        storage = SQLiteStorage(db_path=str(db_path))

        assert db_path.exists()
        # Verify it works
        assert storage.session_count() == 0

    def test_schema_migration_client_id_column(self, temp_db):
        """Test that client_id column exists in schema."""
        # This is implicitly tested by using the storage,
        # but we verify the column exists
        storage = SQLiteStorage(db_path=temp_db)

        now = datetime.now()
        session = Session(
            id="test",
            display_id="test-display",
            name="test",
            machine="localhost",
            cwd="/test",
            repo="test",
            registered_at=now,
            last_heartbeat=now,
            client_id="abc123",
        )
        storage.add_session(session)

        retrieved = storage.get_session("test")
        assert retrieved.client_id == "abc123"

    def test_schema_migration_channel_column(self, temp_db):
        """Test that channel column is added to existing schema."""
        storage = SQLiteStorage(db_path=temp_db)

        storage.add_event(
            event_type="test",
            payload="test",
            session_id="s1",
            channel="repo:myrepo",
        )

        events, _ = storage.get_events()
        assert len(events) == 1
        assert events[0].channel == "repo:myrepo"

    def test_composite_index_on_machine_client_id(self, temp_db):
        """Test that composite index on (machine, client_id) exists for session dedup."""
        import sqlite3

        # Initialize DB to create schema and indexes
        SQLiteStorage(db_path=temp_db)

        # Query SQLite for the index
        conn = sqlite3.connect(temp_db)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='sessions'"
        )
        index_names = {row[0] for row in cursor.fetchall()}
        conn.close()

        assert "idx_sessions_dedup" in index_names, (
            f"Expected idx_sessions_dedup index, found: {index_names}"
        )

        # Verify the index columns
        conn = sqlite3.connect(temp_db)
        cursor = conn.execute("PRAGMA index_info(idx_sessions_dedup)")
        columns = [row[2] for row in cursor.fetchall()]
        conn.close()

        assert columns == ["machine", "client_id"], (
            f"Expected index on (machine, client_id), found: {columns}"
        )

    def test_migrate_v1_to_v2_schema(self, tmp_path):
        """Test v1→v2 migration adds display_id and deleted_at columns."""
        import sqlite3

        db_path = tmp_path / "v1_test.db"

        # Create v1 schema manually (without display_id and deleted_at)
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY
            )
        """)
        conn.execute("INSERT INTO schema_version VALUES (1)")
        conn.execute("""
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                machine TEXT NOT NULL,
                cwd TEXT NOT NULL,
                repo TEXT NOT NULL,
                registered_at TIMESTAMP NOT NULL,
                last_heartbeat TIMESTAMP NOT NULL,
                client_id TEXT,
                last_cursor TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                session_id TEXT NOT NULL,
                timestamp TIMESTAMP NOT NULL,
                channel TEXT NOT NULL DEFAULT 'all'
            )
        """)
        # Add a v1 session (using human-readable ID as was done before v2)
        conn.execute("""
            INSERT INTO sessions (id, name, machine, cwd, repo, registered_at, last_heartbeat, client_id)
            VALUES ('brave-tiger', 'test-session', 'localhost', '/test', 'test-repo',
                    '2024-01-01 12:00:00', '2024-01-01 12:00:00', 'client-123')
        """)
        conn.commit()
        conn.close()

        # Open with SQLiteStorage - should trigger v1→v2 migration
        storage = SQLiteStorage(db_path=str(db_path))

        # Verify migration added columns
        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute("PRAGMA table_info(sessions)")
        columns = {row[1] for row in cursor.fetchall()}
        conn.close()

        assert "display_id" in columns, "display_id column should be added by migration"
        assert "deleted_at" in columns, "deleted_at column should be added by migration"

        # Verify the session's display_id was populated from the old id
        # (The migration copies id → display_id, then may change id if client_id exists)
        sessions = storage.list_sessions()
        assert len(sessions) == 1
        session = sessions[0]
        assert session.display_id == "brave-tiger", "display_id should be populated from old id"
        # Since client_id was set, the new id should be the client_id
        assert session.id == "client-123", "id should become client_id after migration"

        # Verify schema version was updated
        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute("SELECT version FROM schema_version")
        version = cursor.fetchone()[0]
        conn.close()
        assert version == 2, f"Schema version should be 2, got {version}"


class TestSoftDelete:
    """Tests for soft-delete behavior."""

    def test_soft_delete_sets_deleted_at_and_preserves_row(self, storage, temp_db):
        """Verify soft-delete sets deleted_at without removing the row."""
        import sqlite3

        now = datetime.now()
        session = Session(
            id="soft-delete-test",
            display_id="soft-display",
            name="test-session",
            machine="localhost",
            cwd="/test",
            repo="test",
            registered_at=now,
            last_heartbeat=now,
        )
        storage.add_session(session)

        # Verify session exists
        assert storage.get_session("soft-delete-test") is not None

        # Delete the session
        storage.delete_session("soft-delete-test")

        # Verify invisible via normal API
        assert storage.get_session("soft-delete-test") is None
        assert storage.session_count() == 0

        # Verify row still exists with deleted_at set (query DB directly)
        conn = sqlite3.connect(temp_db)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT id, deleted_at FROM sessions WHERE id = ?",
            ("soft-delete-test",),
        )
        row = cursor.fetchone()
        conn.close()

        assert row is not None, "Row should still exist after soft-delete"
        assert row["deleted_at"] is not None, "deleted_at should be set"


class TestDbLocationMigration:
    """Tests for database location migration."""

    def test_migrate_db_from_old_to_new_location(self, tmp_path, monkeypatch):
        """Test database migration moves file from old to new location."""
        import event_bus.storage as storage_module

        old_path = tmp_path / ".claude" / "event-bus.db"
        new_path = tmp_path / ".claude" / "contrib" / "event-bus" / "data.db"

        # Create old-style DB with proper schema (v1 style)
        old_path.parent.mkdir(parents=True)
        conn = sqlite3.connect(str(old_path))
        conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO schema_version VALUES (1)")
        conn.execute("""
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                machine TEXT NOT NULL,
                cwd TEXT NOT NULL,
                repo TEXT NOT NULL,
                registered_at TIMESTAMP NOT NULL,
                last_heartbeat TIMESTAMP NOT NULL,
                client_id TEXT,
                last_cursor TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                session_id TEXT NOT NULL,
                timestamp TIMESTAMP NOT NULL,
                channel TEXT NOT NULL DEFAULT 'all'
            )
        """)
        conn.close()

        # Monkeypatch the paths
        monkeypatch.setattr(storage_module, "OLD_DB_PATH", old_path)
        monkeypatch.setattr(storage_module, "DEFAULT_DB_PATH", new_path)

        # Initialize storage with the new default path - should trigger migration
        storage = SQLiteStorage(db_path=str(new_path))

        # Verify migration occurred
        assert new_path.exists(), "New DB should exist after migration"
        assert not old_path.exists(), "Old DB should be moved (not exist)"

        # Verify storage is functional after migration
        assert storage.session_count() == 0, "Storage should work after migration"

    def test_no_migration_when_old_db_missing(self, tmp_path, monkeypatch):
        """Test that no migration occurs if old DB doesn't exist."""
        import event_bus.storage as storage_module

        old_path = tmp_path / ".claude" / "event-bus.db"
        new_path = tmp_path / ".claude" / "contrib" / "event-bus" / "data.db"

        # Don't create old DB
        monkeypatch.setattr(storage_module, "OLD_DB_PATH", old_path)
        monkeypatch.setattr(storage_module, "DEFAULT_DB_PATH", new_path)

        # Initialize storage - should create fresh DB
        SQLiteStorage(db_path=str(new_path))

        assert new_path.exists(), "New DB should be created"
        assert not old_path.exists(), "Old DB should still not exist"

    def test_no_migration_when_new_db_already_exists(self, tmp_path, monkeypatch):
        """Test that migration is skipped if new DB already exists."""
        import event_bus.storage as storage_module

        old_path = tmp_path / ".claude" / "event-bus.db"
        new_path = tmp_path / ".claude" / "contrib" / "event-bus" / "data.db"

        # Create old DB
        old_path.parent.mkdir(parents=True)
        conn = sqlite3.connect(str(old_path))
        conn.execute("CREATE TABLE old_marker (id INTEGER)")
        conn.close()

        # Create new DB with current schema (so SQLiteStorage doesn't fail)
        new_path.parent.mkdir(parents=True)
        conn = sqlite3.connect(str(new_path))
        conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO schema_version VALUES (2)")
        conn.execute("""
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                display_id TEXT,
                name TEXT NOT NULL,
                machine TEXT NOT NULL,
                cwd TEXT NOT NULL,
                repo TEXT NOT NULL,
                registered_at TIMESTAMP NOT NULL,
                last_heartbeat TIMESTAMP NOT NULL,
                client_id TEXT,
                last_cursor TEXT,
                deleted_at TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                session_id TEXT NOT NULL,
                timestamp TIMESTAMP NOT NULL,
                channel TEXT NOT NULL DEFAULT 'all'
            )
        """)
        conn.close()

        monkeypatch.setattr(storage_module, "OLD_DB_PATH", old_path)
        monkeypatch.setattr(storage_module, "DEFAULT_DB_PATH", new_path)

        # Initialize storage - should NOT overwrite existing new DB
        SQLiteStorage(db_path=str(new_path))

        # Old DB should still exist (not moved because new already exists)
        assert old_path.exists(), "Old DB should still exist when migration skipped"

        # Verify new DB doesn't have old_marker table (wasn't overwritten)
        conn = sqlite3.connect(str(new_path))
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='old_marker'"
        )
        row = cursor.fetchone()
        conn.close()
        assert row is None, "New DB should not have old_marker table (wasn't overwritten)"
