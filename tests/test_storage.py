"""Tests for SQLite storage backend."""

import sqlite3
from datetime import datetime, timedelta

import pytest

from agent_event_bus.storage import SCHEMA_VERSION, SESSION_TIMEOUT, Session, SQLiteStorage


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

    def test_find_session_by_client_excludes_deleted_by_default(self, storage):
        """Test that soft-deleted sessions are excluded by default."""
        now = datetime.now()
        session = Session(
            id="test-123",
            display_id="cool-cat",
            name="test-session",
            machine="localhost",
            cwd="/test",
            repo="test",
            registered_at=now,
            last_heartbeat=now,
            client_id="12345",
        )
        storage.add_session(session)
        storage.delete_session("test-123")

        assert storage.find_session_by_client("localhost", "12345") is None

    def test_find_session_by_client_include_deleted(self, storage):
        """Test that include_deleted=True finds soft-deleted sessions."""
        now = datetime.now()
        session = Session(
            id="test-123",
            display_id="cool-cat",
            name="test-session",
            machine="localhost",
            cwd="/test",
            repo="test",
            registered_at=now,
            last_heartbeat=now,
            client_id="12345",
        )
        storage.add_session(session)
        storage.delete_session("test-123")

        found = storage.find_session_by_client("localhost", "12345", include_deleted=True)
        assert found is not None
        assert found.id == "test-123"
        assert found.display_id == "cool-cat"

    def test_find_session_by_client_include_deleted_prefers_active(self, storage):
        """Test that include_deleted=True prefers active over deleted sessions."""
        now = datetime.now()

        # Create and soft-delete a session
        old_session = Session(
            id="old-123",
            display_id="old-cat",
            name="old-session",
            machine="localhost",
            cwd="/test",
            repo="test",
            registered_at=now,
            last_heartbeat=now,
            client_id="12345",
        )
        storage.add_session(old_session)
        storage.delete_session("old-123")

        # Create an active session with same machine+client_id but different id.
        # This could happen via a race condition between concurrent registrations
        # or manual DB edits. Tests that the ORDER BY prefers active over deleted.
        new_session = Session(
            id="new-123",
            display_id="new-cat",
            name="new-session",
            machine="localhost",
            cwd="/test",
            repo="test",
            registered_at=now,
            last_heartbeat=now,
            client_id="12345",
        )
        storage.add_session(new_session)

        found = storage.find_session_by_client("localhost", "12345", include_deleted=True)
        assert found is not None
        assert found.id == "new-123"  # Active session preferred


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

        events, next_cursor, _ = storage.get_events()
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
        events, next_cursor, _ = storage.get_events(cursor=str(event_ids[2]), order="asc")
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

        events, next_cursor, _ = storage.get_events(limit=3)
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
        events, _, _ = storage.get_events(cursor="not-a-number")
        assert len(events) == 3

        # Empty cursor works normally
        events, _, _ = storage.get_events(cursor="")
        assert len(events) == 3

        # Valid cursor works normally
        events, _, _ = storage.get_events(cursor="1", order="asc")
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
        events, _, _ = storage.get_events(channels=["all", "session:abc", "repo:myrepo"])
        assert len(events) == 3
        types = {e.event_type for e in events}
        assert types == {"broadcast", "direct", "repo"}

    def test_get_events_no_channel_filter(self, storage):
        """Test getting all events when no channel filter is provided."""
        storage.add_event("e1", "msg1", "s1", channel="all")
        storage.add_event("e2", "msg2", "s1", channel="session:abc")
        storage.add_event("e3", "msg3", "s1", channel="repo:myrepo")

        # No channel filter = all events
        events, _, _ = storage.get_events(channels=None)
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
        events, _, _ = storage.get_events(event_types=["task_completed", "ci_completed"])
        assert len(events) == 3
        types = {e.event_type for e in events}
        assert types == {"task_completed", "ci_completed"}

    def test_get_events_single_event_type(self, storage):
        """Test filtering for a single event type."""
        storage.add_event("task_completed", "task 1", "s1")
        storage.add_event("ci_completed", "CI 1", "s1")
        storage.add_event("task_completed", "task 2", "s1")

        events, _, _ = storage.get_events(event_types=["gotcha_discovered"])
        assert len(events) == 0

        events, _, _ = storage.get_events(event_types=["task_completed"])
        assert len(events) == 2
        assert all(e.event_type == "task_completed" for e in events)

    def test_get_events_no_type_filter(self, storage):
        """Test getting all events when no event_types filter is provided."""
        storage.add_event("e1", "msg1", "s1")
        storage.add_event("e2", "msg2", "s1")
        storage.add_event("e3", "msg3", "s1")

        # No event_types filter = all events
        events, _, _ = storage.get_events(event_types=None)
        assert len(events) == 3

    def test_get_events_combined_filters(self, storage):
        """Test combining event_types with channel filter."""
        storage.add_event("task_completed", "task 1", "s1", channel="repo:myrepo")
        storage.add_event("ci_completed", "CI 1", "s1", channel="repo:myrepo")
        storage.add_event("task_completed", "task 2", "s1", channel="all")
        storage.add_event("gotcha_discovered", "gotcha", "s1", channel="repo:myrepo")

        # Filter by both channel and event type
        events, _, _ = storage.get_events(
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

        events, _, _ = storage.get_events()
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
        # Schema version should be the latest after all migrations run
        from agent_event_bus.storage import SCHEMA_VERSION

        assert version == SCHEMA_VERSION, (
            f"Schema version should be {SCHEMA_VERSION}, got {version}"
        )


# (table, column) pairs whose NOT NULL / DEFAULT is KNOWN to differ between a
# fresh CREATE and a migrated database, and is accepted as-is. Only the
# declared type is compared for these.
#
# sessions.display_id: _init_db declares it TEXT NOT NULL, but migration v2
# can only add it with a bare `ALTER TABLE ... ADD COLUMN display_id TEXT` -
# SQLite has no ALTER COLUMN, and v2's own closing comment records the
# decision to enforce the constraint in application code instead. Closing it
# needs a full table rebuild on live user data; that is its own change, not a
# line in a cleanup pass. This list is the honest boundary of the guard - a
# NOT NULL or DEFAULT divergence anywhere NOT listed here fails.
KNOWN_CONSTRAINT_DIVERGENCES = {("sessions", "display_id")}


def _schema_snapshot(db_path) -> dict:
    """Per table: columns as name → (type, notnull, pk, default), and indexes
    as name → CREATE statement.

    That tuple order is load-bearing: _compare_columns indexes [0] for the
    declared type and [1:] for the constraints it relaxes on an exempt column.

    Column ORDER is deliberately not compared: ALTER TABLE appends, so a
    migrated database legitimately orders columns differently from a fresh
    CREATE TABLE. Everything else that affects queries is compared - notnull
    and default because a constraint landing in one path only is exactly the
    drift this guards, and the index SQL because comparing index NAMES alone
    would let an index over the wrong columns match.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        snapshot = {}
        for table in tables:
            # PRAGMA table_info rows: (cid, name, type, notnull, dflt_value, pk)
            columns = {
                row[1]: (row[2].upper(), row[3], row[5], row[4])
                for row in conn.execute(f"PRAGMA table_info({table})")
            }
            indexes = {
                row[0]: row[1]
                for row in conn.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name=? "
                    "AND name NOT LIKE 'sqlite_%'",
                    (table,),
                )
            }
            snapshot[table] = {"columns": columns, "indexes": indexes}
        return snapshot
    finally:
        conn.close()


def _compare_columns(table: str, migrated: dict, fresh: dict) -> None:
    """Assert column parity, allowing only the recorded constraint divergences."""
    assert migrated.keys() == fresh.keys(), (
        f"Column sets diverged on '{table}': "
        f"only in fresh={sorted(fresh.keys() - migrated.keys())}, "
        f"only in migrated={sorted(migrated.keys() - fresh.keys())}"
    )
    for column, fresh_spec in fresh.items():
        migrated_spec = migrated[column]
        if (table, column) in KNOWN_CONSTRAINT_DIVERGENCES:
            assert migrated_spec[0] == fresh_spec[0], (
                f"'{table}.{column}' is an accepted CONSTRAINT divergence, but its "
                f"declared TYPE diverged too: {migrated_spec[0]} vs {fresh_spec[0]}"
            )
            continue
        assert migrated_spec == fresh_spec, (
            f"'{table}.{column}' diverged (type, notnull, pk, default): "
            f"migrated={migrated_spec}, fresh={fresh_spec}. A schema change landed "
            f"in one of _init_db / the @migration registry but not the other."
        )


class TestSchemaParity:
    """A fresh install and a migrated database must end up with ONE schema.

    _init_db creates the current schema for fresh installs while the
    @migration registry upgrades existing databases incrementally - two
    independent definitions of the same thing, and _init_db's docstring can
    only assert they agree. This test is the mechanism behind that assertion:
    add a column to one path and forget the other, and it fails here rather
    than as a "no such column" on someone's live bus.
    """

    def _build_v1_db(self, db_path):
        """A v1 database predating every column later code bolted on.

        Deliberately omits sessions.last_cursor and events.channel - the two
        that used to be added by inline try/except ALTERs in _init_db and are
        now migration 5. Building the fixture WITH them would let that
        migration rot unnoticed while this test stayed green.
        """
        conn = sqlite3.connect(str(db_path))
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
                client_id TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                session_id TEXT NOT NULL,
                timestamp TIMESTAMP NOT NULL
            )
        """)
        conn.commit()
        conn.close()

    def test_migrated_v1_matches_fresh_install(self, tmp_path):
        fresh_path = tmp_path / "fresh.db"
        SQLiteStorage(db_path=str(fresh_path))

        migrated_path = tmp_path / "migrated.db"
        self._build_v1_db(migrated_path)
        SQLiteStorage(db_path=str(migrated_path))

        fresh = _schema_snapshot(fresh_path)
        migrated = _schema_snapshot(migrated_path)

        assert migrated.keys() == fresh.keys(), (
            f"Table sets diverged: fresh={sorted(fresh)}, migrated={sorted(migrated)}"
        )
        for table in fresh:
            _compare_columns(table, migrated[table]["columns"], fresh[table]["columns"])
            assert migrated[table]["indexes"] == fresh[table]["indexes"], (
                f"Indexes diverged on '{table}' (name → CREATE statement)."
            )

    def test_no_new_divergences_have_been_recorded(self):
        """Watches the list GROWING. Deliberately a change-detector.

        Silencing a real parity failure by adding its column here is the easy
        wrong move, and nothing else would catch it: the parity test relaxes
        for whatever is listed, and the staleness test below would confirm the
        new entry genuinely diverges - which is precisely why someone added
        it. Widening the guard's blind spot should cost a deliberate edit and
        a written reason, not a one-word append.
        """
        assert KNOWN_CONSTRAINT_DIVERGENCES == {("sessions", "display_id")}, (
            "KNOWN_CONSTRAINT_DIVERGENCES changed. Adding an entry widens a blind "
            "spot in TestSchemaParity - do it only for a divergence that genuinely "
            "cannot be migrated away, document why in the comment above the "
            "constant, and update this test to match."
        )

    def test_the_recorded_divergences_are_still_real(self, tmp_path):
        """Watches the list going STALE - the opposite direction to the test
        above, and the reason a change-detector alone is not enough here.

        Every recorded allowance must still be earning its place.

        OBSERVES the divergence rather than asserting the constant against its
        own literal. The literal form would be a change-detector: a rebuild
        migration that made display_id NOT NULL on migrated databases too
        would leave the constant untouched (green here) and the parity test
        green as well - an exemption only ever relaxes a comparison - so the
        stale allowance would survive precisely the event meant to retire it,
        and its blind spot would be permanent from then on.

        Compares everything except the declared type, since type is compared
        for exempt columns anyway: an entry whose (notnull, pk, default) all
        match is exempting nothing.
        """
        fresh_path = tmp_path / "fresh.db"
        SQLiteStorage(db_path=str(fresh_path))

        migrated_path = tmp_path / "migrated.db"
        self._build_v1_db(migrated_path)
        SQLiteStorage(db_path=str(migrated_path))

        fresh = _schema_snapshot(fresh_path)
        migrated = _schema_snapshot(migrated_path)

        for table, column in KNOWN_CONSTRAINT_DIVERGENCES:
            # Retirement has two shapes and both must reach the same
            # conclusion. The constraint converging is handled below; the
            # column or table going away is handled here, because a rebuild
            # migration - the standard SQLite create-copy-drop-rename dance,
            # and the likeliest way display_id ever gets fixed - would
            # otherwise surface as a bare KeyError with the reasoning lost.
            for label, snapshot in (("migrated", migrated), ("fresh", fresh)):
                assert table in snapshot, (
                    f"KNOWN_CONSTRAINT_DIVERGENCES names table '{table}', which no "
                    f"longer exists in a {label} database - the entry is stale, delete it."
                )
                assert column in snapshot[table]["columns"], (
                    f"KNOWN_CONSTRAINT_DIVERGENCES names '{table}.{column}', and that "
                    f"column no longer exists in a {label} database - the entry is "
                    f"stale, delete it."
                )

            migrated_spec = migrated[table]["columns"][column]
            fresh_spec = fresh[table]["columns"][column]
            assert migrated_spec[1:] != fresh_spec[1:], (
                f"'{table}.{column}' no longer diverges between a fresh and a migrated "
                f"database (both {fresh_spec[1:]} for notnull/pk/default). The entry in "
                f"KNOWN_CONSTRAINT_DIVERGENCES is now exempting nothing - delete it, so "
                f"the column goes back to being compared in full."
            )

    def test_reopening_is_idempotent(self, tmp_path):
        """Re-opening an up-to-date database must not re-run or re-alter anything."""
        db_path = tmp_path / "reopen.db"
        SQLiteStorage(db_path=str(db_path))
        first = _schema_snapshot(db_path)

        storage = SQLiteStorage(db_path=str(db_path))
        assert _schema_snapshot(db_path) == first

        # And exactly one version row survives (earlier versions accumulated rows)
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT version FROM schema_version").fetchall()
        conn.close()
        assert rows == [(SCHEMA_VERSION,)]
        assert storage.session_count() == 0


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


class TestLegacyDbLocation:
    """A database left at a pre-rename path is reported, never moved.

    The automatic move was removed: it relocated the user's only copy, on a
    path this code has not written to since January, using a plain file move
    that is unsafe for a WAL-era database. Reporting is what remains - and it
    has to happen, or a stale old-path database would present as a brand-new
    empty bus with the real history sitting unnoticed on disk.
    """

    def _legacy_paths(self, tmp_path, monkeypatch, *, which="old"):
        import agent_event_bus.storage as storage_module

        old_path = tmp_path / ".claude" / "event-bus.db"
        contrib_path = tmp_path / ".claude" / "contrib" / "event-bus" / "data.db"
        new_path = tmp_path / ".claude" / "contrib" / "agent-event-bus" / "data.db"

        monkeypatch.setattr(storage_module, "OLD_DB_PATH", old_path)
        monkeypatch.setattr(storage_module, "OLD_CONTRIB_DB_PATH", contrib_path)
        monkeypatch.setattr(storage_module, "DEFAULT_DB_PATH", new_path)
        return old_path, contrib_path, new_path

    def test_legacy_db_is_left_in_place_and_reported(self, tmp_path, monkeypatch, caplog):
        import logging

        old_path, _, new_path = self._legacy_paths(tmp_path, monkeypatch)
        old_path.parent.mkdir(parents=True)
        old_path.write_bytes(b"not really a database, and never opened")

        with caplog.at_level(logging.WARNING, logger="agent-event-bus"):
            storage = SQLiteStorage(db_path=str(new_path))

        assert old_path.exists(), "the legacy database must not be moved or removed"
        assert old_path.read_bytes() == b"not really a database, and never opened"
        assert new_path.exists()
        assert storage.session_count() == 0

        warning = "\n".join(r.message for r in caplog.records)
        assert str(old_path) in warning
        assert "EMPTY" in warning
        assert ".backup" in warning, "the hint must be the WAL-aware command, not cp"

    def test_contrib_path_takes_precedence(self, tmp_path, monkeypatch, caplog):
        """Both legacy paths present: name the newer one, and only it."""
        import logging

        old_path, contrib_path, new_path = self._legacy_paths(tmp_path, monkeypatch)
        old_path.parent.mkdir(parents=True)
        old_path.write_bytes(b"older")
        contrib_path.parent.mkdir(parents=True)
        contrib_path.write_bytes(b"newer")

        with caplog.at_level(logging.WARNING, logger="agent-event-bus"):
            SQLiteStorage(db_path=str(new_path))

        warning = "\n".join(r.message for r in caplog.records)
        assert str(contrib_path) in warning
        assert str(old_path) not in warning

    def test_silent_when_no_legacy_db_exists(self, tmp_path, monkeypatch, caplog):
        import logging

        _, _, new_path = self._legacy_paths(tmp_path, monkeypatch)

        with caplog.at_level(logging.WARNING, logger="agent-event-bus"):
            SQLiteStorage(db_path=str(new_path))

        assert new_path.exists()
        assert not [r for r in caplog.records if "pre-rename" in r.message]

    def test_warning_repeats_while_the_current_db_is_still_empty(
        self, tmp_path, monkeypatch, caplog
    ):
        """Keyed on emptiness, not existence.

        Gating on db_path.exists() would warn on exactly one boot - the one
        that creates the file - and then go quiet forever while the bus ran
        empty and the real history sat at the old path.
        """
        import logging

        old_path, _, new_path = self._legacy_paths(tmp_path, monkeypatch)
        old_path.parent.mkdir(parents=True)
        old_path.write_bytes(b"stale")

        for restart in range(3):
            caplog.clear()
            with caplog.at_level(logging.WARNING, logger="agent-event-bus"):
                SQLiteStorage(db_path=str(new_path))
            assert [r for r in caplog.records if "pre-rename" in r.message], (
                f"restart {restart}: the warning must persist while it is actionable"
            )

    def test_warning_stops_once_real_history_accumulates(self, tmp_path, monkeypatch, caplog):
        """Self-limiting: one real event here and the operator has moved on."""
        import logging

        old_path, _, new_path = self._legacy_paths(tmp_path, monkeypatch)
        old_path.parent.mkdir(parents=True)
        old_path.write_bytes(b"stale")

        storage = SQLiteStorage(db_path=str(new_path))
        storage.add_event(event_type="task_completed", payload="real history", session_id="s1")

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="agent-event-bus"):
            SQLiteStorage(db_path=str(new_path))

        assert not [r for r in caplog.records if "pre-rename" in r.message]


class TestPrehistoricSchemaRefusal:
    """A pid-based sessions table is refused, not silently dropped.

    The old code ran an unconditional DROP TABLE sessions on this shape. Such
    a database cannot realistically exist any more, but if one does, the
    operator's rows are theirs to lose - not ours.
    """

    def _build_pid_schema_db(self, db_path):
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                machine TEXT NOT NULL,
                cwd TEXT NOT NULL,
                repo TEXT NOT NULL,
                registered_at TIMESTAMP NOT NULL,
                last_heartbeat TIMESTAMP NOT NULL,
                pid INTEGER
            )
        """)
        conn.execute("""
            INSERT INTO sessions VALUES
            ('old-1', 'legacy', 'localhost', '/tmp', 'tmp', '2026-01-02', '2026-01-02', 4242)
        """)
        conn.commit()
        conn.close()

    def test_refuses_and_preserves_the_rows(self, tmp_path):
        db_path = tmp_path / "prehistoric.db"
        self._build_pid_schema_db(db_path)

        with pytest.raises(RuntimeError, match="pid-based"):
            SQLiteStorage(db_path=str(db_path))

        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT id FROM sessions").fetchall()
        tables = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = ?", ("table",))
        }
        conn.close()

        assert rows == [("old-1",)], "the refusal must not have touched user rows"
        # Pins the ORDER of the check inside _init_db, which its docstring
        # promises ("runs before _init_db creates anything") and which the row
        # assertion above cannot see - rows survived under the old ordering
        # too. Move the call back below the schema_version CREATE and this is
        # the assertion that fails.
        assert tables == {"sessions"}, (
            f"the refusal created scaffolding of its own: {sorted(tables)}. Only "
            f"journal_mode=WAL may change on a refused database - format, not content."
        )

    def test_current_schema_is_unaffected(self, tmp_path):
        """The guard keys on pid-without-client_id; a normal database opens."""
        db_path = tmp_path / "normal.db"
        SQLiteStorage(db_path=str(db_path))
        SQLiteStorage(db_path=str(db_path))  # must not raise on reopen


class TestNextCursorHighWater:
    """next_cursor must never re-serve events, regardless of order."""

    def _add_events(self, storage, n):
        return [
            storage.add_event(event_type=f"event_{i}", payload=f"payload {i}", session_id="s1").id
            for i in range(n)
        ]

    def test_desc_next_cursor_is_high_water(self, storage):
        ids = self._add_events(storage, 5)

        events, next_cursor, _ = storage.get_events(order="desc")
        assert next_cursor == str(max(ids))

    def test_desc_polling_with_next_cursor_never_duplicates(self, storage):
        """The old MIN-for-desc cursor returned the same newest events on
        every follow-up call; feeding next_cursor back must yield only new
        events."""
        self._add_events(storage, 3)
        _, cursor, _ = storage.get_events(order="desc")

        # No new events: nothing comes back
        events, cursor2, _ = storage.get_events(cursor=cursor, order="desc")
        assert events == []
        assert cursor2 == cursor

        # A new event: exactly that event comes back
        new_id = storage.add_event(event_type="fresh", payload="new", session_id="s1").id
        events, cursor3, _ = storage.get_events(cursor=cursor, order="desc")
        assert [e.id for e in events] == [new_id]
        assert cursor3 == str(new_id)

    def test_asc_next_cursor_unchanged(self, storage):
        ids = self._add_events(storage, 5)

        events, next_cursor, _ = storage.get_events(order="asc")
        assert next_cursor == str(max(ids))


class TestBacklogPaging:
    """has_more semantics when the unseen backlog exceeds limit."""

    def _add_events(self, storage, n):
        return [
            storage.add_event(event_type=f"event_{i}", payload=f"payload {i}", session_id="s1").id
            for i in range(n)
        ]

    def test_desc_backlog_beyond_limit_sets_has_more_and_skips(self, storage):
        ids = self._add_events(storage, 7)

        events, next_cursor, has_more = storage.get_events(limit=5, order="desc")
        assert [e.id for e in events] == list(reversed(ids[-5:]))
        assert has_more is True
        assert next_cursor == str(max(ids))

        # Documented desc trade-off: the page is the newest slice, so feeding
        # next_cursor back skips the two oldest backlog events. Drain with
        # order="asc" when no event may be missed.
        events2, _, has_more2 = storage.get_events(cursor=next_cursor, limit=5, order="desc")
        assert events2 == []
        assert has_more2 is False

    def test_asc_drains_backlog_across_pages(self, storage):
        ids = self._add_events(storage, 7)

        seen = []
        cursor = None
        for _ in range(10):
            events, cursor, has_more = storage.get_events(cursor=cursor, limit=5, order="asc")
            seen.extend(e.id for e in events)
            if not has_more:
                break

        assert seen == ids

    def test_limit_zero_does_not_report_more(self, storage):
        """limit=0 returns an empty page that never advances the cursor; a
        keep-polling-while-has_more loop would spin forever if it were True."""
        self._add_events(storage, 3)

        events, cursor, has_more = storage.get_events(limit=0)
        assert events == []
        assert has_more is False
