"""SQLite storage backend for event bus persistence."""

import logging
import os
import shutil
import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

logger = logging.getLogger("event-bus")

# Schema version for migrations
# Increment this when adding new migrations
SCHEMA_VERSION = 2

# Migration function type: takes a connection, returns nothing
MigrationFunc = Callable[[sqlite3.Connection], None]

# Migration registry: version -> (name, migration_function)
MIGRATIONS: dict[int, tuple[str, MigrationFunc]] = {}


def migration(version: int, name: str):
    """Decorator to register a schema migration.

    Usage:
        @migration(2, "add_some_column")
        def migrate_v2(conn: sqlite3.Connection) -> None:
            conn.execute("ALTER TABLE ... ADD COLUMN ...")
    """

    def decorator(func: MigrationFunc):
        MIGRATIONS[version] = (name, func)
        return func

    return decorator


# Migration for UUID-based session IDs and soft-delete
@migration(2, "uuid_session_ids_and_soft_delete")
def migrate_v2(conn: sqlite3.Connection) -> None:
    """Add display_id and deleted_at columns to sessions table.

    This migration:
    1. Adds display_id column (human-readable name like "brave-tiger")
    2. Adds deleted_at column (for soft-delete)
    3. Copies existing id → display_id
    4. Changes id to use client_id (if available) or generates UUID

    Note on historical data: Existing events retain their old session_id references
    (human-readable names like "brave-tiger"). These become orphaned - they no longer
    match any session's primary key. This is expected: the middleware handles display
    of historical events via _is_human_readable_id() fallback. New events will use
    the new UUID-based session_id.
    """
    import uuid

    # Check which columns already exist (fresh DB vs upgrade)
    cursor = conn.execute("PRAGMA table_info(sessions)")
    existing_columns = {row[1] for row in cursor.fetchall()}

    # Add new columns only if they don't exist
    if "display_id" not in existing_columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN display_id TEXT")
    if "deleted_at" not in existing_columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN deleted_at TIMESTAMP")

    # Copy existing id to display_id ONLY if display_id is NULL or same as id (UUID)
    # This preserves human-readable display_ids from new session registrations
    # while still populating display_id for legacy sessions from before this column existed
    conn.execute("""
        UPDATE sessions
        SET display_id = id
        WHERE display_id IS NULL
    """)

    # For sessions with client_id, update id to use client_id
    # For sessions without client_id, generate a UUID
    rows = conn.execute("SELECT id, client_id FROM sessions").fetchall()
    for row in rows:
        old_id = row[0]
        client_id = row[1]

        if client_id:
            new_id = client_id
        else:
            new_id = str(uuid.uuid4())

        # Update the session id (SQLite allows this even for PK)
        conn.execute(
            "UPDATE sessions SET id = ? WHERE id = ?",
            (new_id, old_id),
        )

    # Make display_id NOT NULL now that all rows have values
    # SQLite doesn't support ALTER COLUMN, so we'll enforce in application


# Register datetime adapters/converters (required for Python 3.12+)
# See: https://docs.python.org/3/library/sqlite3.html#default-adapters-and-converters-deprecated


def _adapt_datetime(dt: datetime) -> str:
    """Convert datetime to ISO format string for SQLite storage."""
    return dt.isoformat()


def _convert_datetime(data: bytes) -> datetime:
    """Convert ISO format string from SQLite to datetime."""
    return datetime.fromisoformat(data.decode())


sqlite3.register_adapter(datetime, _adapt_datetime)
sqlite3.register_converter("TIMESTAMP", _convert_datetime)


@dataclass
class Session:
    """Represents an active Claude Code session."""

    id: str  # UUID or client_id (stable identifier for API use)
    display_id: str  # Human-readable name like "brave-tiger" (for display only)
    name: str
    machine: str
    cwd: str
    repo: str
    registered_at: datetime
    last_heartbeat: datetime
    client_id: str | None = None  # Client identifier for session deduplication
    last_cursor: str | None = None  # Last seen event cursor for this session
    deleted_at: datetime | None = None  # Soft-delete timestamp (None = active)

    def get_project_name(self) -> str:
        """Get the project name, preferring explicit repo over cwd basename.

        Returns:
            Project name derived from repo field, or the last directory component of cwd.
            Note: repo field is already sanitized at registration time by extract_repo_from_cwd().
            The fallback path also sanitizes for defense-in-depth.
        """
        if self.repo:
            return self.repo

        if self.cwd:
            # Strip trailing slashes to handle paths like "/path/to/project/"
            basename = os.path.basename(self.cwd.rstrip("/"))
            if basename:
                # Sanitize special chars (defense-in-depth, matches extract_repo_from_cwd)
                return basename.replace("\n", " ").replace("\t", " ").replace("\r", " ")

        return "unknown"


@dataclass
class Event:
    """An event broadcast to all sessions."""

    id: int
    event_type: str
    payload: str
    session_id: str
    timestamp: datetime
    channel: str = "all"  # Target channel for the event


# Database paths
# New canonical path (aligned with session-analytics under contrib/)
DEFAULT_DB_PATH = Path.home() / ".claude" / "contrib" / "event-bus" / "data.db"
# Old path for automatic migration
OLD_DB_PATH = Path.home() / ".claude" / "event-bus.db"

# Session timeout in seconds (24 hours without activity = dead)
# Local crashed sessions are cleaned up faster via client liveness check
# in list_sessions()
SESSION_TIMEOUT = 86400  # 24 hours


class SQLiteStorage:
    """SQLite-backed storage for sessions and events."""

    def __init__(self, db_path: str | None = None):
        """Initialize storage with optional custom DB path."""
        if db_path is None:
            db_path = os.environ.get("EVENT_BUS_DB", str(DEFAULT_DB_PATH))

        self.db_path = Path(db_path)

        # Migrate from old location if needed (only for default path, not custom/test paths)
        if self.db_path == DEFAULT_DB_PATH:
            self._migrate_db_location()

        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._init_db()

    def _migrate_db_location(self) -> None:
        """Migrate database from old location to new location.

        Old: ~/.claude/event-bus.db
        New: ~/.claude/contrib/event-bus/data.db
        """
        if OLD_DB_PATH.exists() and not self.db_path.exists():
            logger.info(f"Migrating database from {OLD_DB_PATH} to {self.db_path}")
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(OLD_DB_PATH), str(self.db_path))
            logger.info("Database migration complete")

    @contextmanager
    def _connect(self):
        """Context manager for database connections."""
        conn = sqlite3.connect(
            self.db_path,
            detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
        )
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _migrate_sessions_schema(self, conn: sqlite3.Connection) -> None:
        """Migrate from old pid-based schema to client_id schema.

        This is a breaking change migration - we drop the old sessions table
        and recreate with the new schema. Approved for clean break in RFC #29.
        """
        # Check if sessions table exists with old schema (pid column)
        cursor = conn.execute("PRAGMA table_info(sessions)")
        columns = {row[1] for row in cursor.fetchall()}

        if "pid" in columns and "client_id" not in columns:
            logger.warning("Migrating sessions table: dropping old pid-based schema")
            conn.execute("DROP TABLE sessions")

    def _get_schema_version(self, conn: sqlite3.Connection) -> int:
        """Get current schema version from database."""
        try:
            # Get MAX version to handle multiple rows (bug from earlier versions)
            row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
            return row[0] if row and row[0] is not None else 0
        except sqlite3.OperationalError:
            # Table doesn't exist yet
            return 0

    def _run_migrations(self, conn: sqlite3.Connection, current_version: int) -> None:
        """Run all pending migrations."""
        for version in range(current_version + 1, SCHEMA_VERSION + 1):
            if version in MIGRATIONS:
                name, migration_func = MIGRATIONS[version]
                logger.info(f"Running migration {version}: {name}")
                migration_func(conn)
        # Clear and set version (handles multi-row bug from earlier versions)
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))

    def _init_db(self):
        """Create tables if they don't exist.

        NOTE: Schema elements are defined here for fresh installs.
        Migrations incrementally upgrade existing databases.
        Both paths must result in identical schemas.
        """
        with self._connect() as conn:
            # Create schema_version table first
            conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY
                )
            """)

            # Check if we need to migrate from pid to client_id schema
            self._migrate_sessions_schema(conn)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    display_id TEXT NOT NULL,
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
            # Add last_cursor column if upgrading from older schema
            try:
                conn.execute("ALTER TABLE sessions ADD COLUMN last_cursor TEXT")
            except sqlite3.OperationalError:
                pass  # Column already exists
            conn.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    timestamp TIMESTAMP NOT NULL,
                    channel TEXT NOT NULL DEFAULT 'all'
                )
            """)
            # Add channel column if upgrading from older schema
            try:
                conn.execute("ALTER TABLE events ADD COLUMN channel TEXT NOT NULL DEFAULT 'all'")
            except sqlite3.OperationalError:
                pass  # Column already exists
            # Index for efficient event polling
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_events_id ON events(id)
            """)
            # Index for efficient session ordering by activity
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_sessions_heartbeat ON sessions(last_heartbeat)
            """)
            # Index for efficient session deduplication lookup (machine, client_id)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_sessions_dedup ON sessions(machine, client_id)
            """)

            # Run any pending migrations (also handles fresh installs where version=0)
            current_version = self._get_schema_version(conn)
            if current_version < SCHEMA_VERSION:
                self._run_migrations(conn, current_version)

    # Session operations

    def add_session(self, session: Session) -> None:
        """Add or update a session."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO sessions
                (id, display_id, name, machine, cwd, repo, registered_at, last_heartbeat,
                 client_id, last_cursor, deleted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.id,
                    session.display_id,
                    session.name,
                    session.machine,
                    session.cwd,
                    session.repo,
                    session.registered_at,
                    session.last_heartbeat,
                    session.client_id,
                    session.last_cursor,
                    session.deleted_at,
                ),
            )

    def find_session_by_client(self, machine: str, client_id: str) -> Session | None:
        """Find an existing active session by machine+client_id key.

        The dedup key is (machine, client_id) because client_ids (like PIDs) are
        machine-local - the same value on different machines represents different clients.

        Only returns active (non-deleted) sessions.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE machine = ? AND client_id = ? AND deleted_at IS NULL",
                (machine, client_id),
            ).fetchone()
            if row:
                return self._row_to_session(row)
            return None

    def _row_to_session(self, row: sqlite3.Row) -> Session:
        """Convert a database row to a Session object."""
        return Session(
            id=row["id"],
            display_id=row["display_id"] if "display_id" in row.keys() else row["id"],
            name=row["name"],
            machine=row["machine"],
            cwd=row["cwd"],
            repo=row["repo"],
            registered_at=row["registered_at"],
            last_heartbeat=row["last_heartbeat"],
            client_id=row["client_id"],
            last_cursor=row["last_cursor"] if "last_cursor" in row.keys() else None,
            deleted_at=row["deleted_at"] if "deleted_at" in row.keys() else None,
        )

    def get_session(self, session_id: str) -> Session | None:
        """Get an active session by ID.

        Only returns active (non-deleted) sessions.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ? AND deleted_at IS NULL",
                (session_id,),
            ).fetchone()
            if row:
                return self._row_to_session(row)
            return None

    def delete_session(self, session_id: str) -> bool:
        """Soft-delete a session by ID.

        Sets deleted_at timestamp instead of removing the row.
        Returns True if the session was deleted, False if not found.
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE sessions SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
                (datetime.now(), session_id),
            )
            return cursor.rowcount > 0

    def update_heartbeat(self, session_id: str, timestamp: datetime) -> bool:
        """Update session heartbeat. Returns True if active session exists."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE sessions SET last_heartbeat = ? WHERE id = ? AND deleted_at IS NULL",
                (timestamp, session_id),
            )
            return cursor.rowcount > 0

    def update_session_cursor(self, session_id: str, cursor: str) -> bool:
        """Update session's last seen cursor. Returns True if active session exists."""
        with self._connect() as conn:
            result = conn.execute(
                "UPDATE sessions SET last_cursor = ? WHERE id = ? AND deleted_at IS NULL",
                (cursor, session_id),
            )
            return result.rowcount > 0

    def list_sessions(self) -> list[Session]:
        """List all active sessions, ordered by most recently active first.

        Only returns active (non-deleted) sessions.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE deleted_at IS NULL ORDER BY last_heartbeat DESC"
            ).fetchall()
            return [self._row_to_session(row) for row in rows]

    def cleanup_stale_sessions(self, timeout_seconds: int = SESSION_TIMEOUT) -> int:
        """Soft-delete sessions that haven't sent a heartbeat recently.

        Returns the number of sessions marked as deleted.
        """
        cutoff = datetime.now().timestamp() - timeout_seconds
        cutoff_dt = datetime.fromtimestamp(cutoff)
        now = datetime.now()

        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE sessions SET deleted_at = ? WHERE last_heartbeat < ? AND deleted_at IS NULL",
                (now, cutoff_dt),
            )
            return cursor.rowcount

    def session_count(self) -> int:
        """Get count of active (non-deleted) sessions."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as count FROM sessions WHERE deleted_at IS NULL"
            ).fetchone()
            return row["count"]

    # Event operations

    def add_event(
        self, event_type: str, payload: str, session_id: str, channel: str = "all"
    ) -> Event:
        """Add a new event and return it with assigned ID."""
        now = datetime.now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO events (event_type, payload, session_id, timestamp, channel)
                VALUES (?, ?, ?, ?, ?)
                """,
                (event_type, payload, session_id, now, channel),
            )
            event_id = cursor.lastrowid

            return Event(
                id=event_id,
                event_type=event_type,
                payload=payload,
                session_id=session_id,
                timestamp=now,
                channel=channel,
            )

    def get_events(
        self,
        cursor: str | None = None,
        limit: int = 50,
        channels: list[str] | None = None,
        order: Literal["asc", "desc"] = "desc",
        event_types: list[str] | None = None,
    ) -> tuple[list[Event], str | None]:
        """Get events with cursor-based pagination.

        Args:
            cursor: Opaque position from previous call. None = start from recent.
            limit: Maximum number of events to return.
            channels: Optional list of channels to filter by (None = all events).
            order: "desc" (newest first, default) or "asc" (oldest first).
            event_types: Optional list of event types to filter by (None = all types).

        Returns:
            Tuple of (events, next_cursor). Use next_cursor for subsequent calls.
            next_cursor is the cursor value if there are events, None otherwise.
        """
        with self._connect() as conn:
            effective_order = "DESC" if order == "desc" else "ASC"

            # Decode cursor to event ID (cursor is opaque string encoding an ID)
            # Handle malformed cursors gracefully by resetting to start
            since_id = 0
            if cursor:
                try:
                    since_id = int(cursor)
                except ValueError:
                    since_id = 0  # Malformed cursor, reset to start

            # Build WHERE clause based on cursor
            if since_id == 0:
                where_clause = ""
                params_base: tuple = ()
            else:
                where_clause = "WHERE id > ?"
                params_base = (since_id,)

            if channels:
                placeholders = ",".join("?" * len(channels))
                channel_filter = f"channel IN ({placeholders})"
                if where_clause:
                    where_clause += f" AND {channel_filter}"
                else:
                    where_clause = f"WHERE {channel_filter}"
                params_base = (*params_base, *channels)

            if event_types and len(event_types) > 0:
                placeholders = ",".join("?" * len(event_types))
                type_filter = f"event_type IN ({placeholders})"
                if where_clause:
                    where_clause += f" AND {type_filter}"
                else:
                    where_clause = f"WHERE {type_filter}"
                params_base = (*params_base, *event_types)

            params = (*params_base, limit)

            query = f"""
                SELECT * FROM events
                {where_clause}
                ORDER BY id {effective_order}
                LIMIT ?
            """
            rows = conn.execute(query, params).fetchall()

            events = [
                Event(
                    id=row["id"],
                    event_type=row["event_type"],
                    payload=row["payload"],
                    session_id=row["session_id"],
                    timestamp=row["timestamp"],
                    channel=row["channel"] if "channel" in row.keys() else "all",
                )
                for row in rows
            ]

            # Compute next_cursor from the events based on order
            # For DESC: next_cursor is the MIN id (oldest in this batch)
            # For ASC: next_cursor is the MAX id (newest in this batch)
            if events:
                if order == "desc":
                    next_cursor = str(min(e.id for e in events))
                else:
                    next_cursor = str(max(e.id for e in events))
            else:
                next_cursor = cursor  # No new events, keep same cursor

            return events, next_cursor

    def get_cursor(self) -> str | None:
        """Get a cursor pointing to the most recent event.

        Returns:
            Cursor string for the latest event, or None if no events exist.
        """
        with self._connect() as conn:
            row = conn.execute("SELECT MAX(id) as max_id FROM events").fetchone()
            max_id = row["max_id"]
            return str(max_id) if max_id else None
