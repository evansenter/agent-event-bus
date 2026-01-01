"""SQLite storage backend for event bus persistence."""

import logging
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

logger = logging.getLogger("event-bus")

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

    id: str
    name: str
    machine: str
    cwd: str
    repo: str
    registered_at: datetime
    last_heartbeat: datetime
    client_id: str | None = None  # Client identifier for session deduplication

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


# Default database path
DEFAULT_DB_PATH = Path.home() / ".claude" / "event-bus.db"

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
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._init_db()

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

    def _init_db(self):
        """Create tables if they don't exist."""
        with self._connect() as conn:
            # Check if we need to migrate from pid to client_id schema
            self._migrate_sessions_schema(conn)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
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

    # Session operations

    def add_session(self, session: Session) -> None:
        """Add or update a session."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO sessions
                (id, name, machine, cwd, repo, registered_at, last_heartbeat, client_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.id,
                    session.name,
                    session.machine,
                    session.cwd,
                    session.repo,
                    session.registered_at,
                    session.last_heartbeat,
                    session.client_id,
                ),
            )

    def find_session_by_client(self, machine: str, client_id: str) -> Session | None:
        """Find an existing session by machine+client_id key.

        The dedup key is (machine, client_id) because client_ids (like PIDs) are
        machine-local - the same value on different machines represents different clients.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE machine = ? AND client_id = ?",
                (machine, client_id),
            ).fetchone()
            if row:
                return self._row_to_session(row)
            return None

    def _row_to_session(self, row: sqlite3.Row) -> Session:
        """Convert a database row to a Session object."""
        return Session(
            id=row["id"],
            name=row["name"],
            machine=row["machine"],
            cwd=row["cwd"],
            repo=row["repo"],
            registered_at=row["registered_at"],
            last_heartbeat=row["last_heartbeat"],
            client_id=row["client_id"],
        )

    def get_session(self, session_id: str) -> Session | None:
        """Get a session by ID."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if row:
                return self._row_to_session(row)
            return None

    def delete_session(self, session_id: str) -> bool:
        """Delete a session by ID.

        Returns True if the session was deleted, False if not found.
        """
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            return cursor.rowcount > 0

    def update_heartbeat(self, session_id: str, timestamp: datetime) -> bool:
        """Update session heartbeat. Returns True if session exists."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE sessions SET last_heartbeat = ? WHERE id = ?",
                (timestamp, session_id),
            )
            return cursor.rowcount > 0

    def list_sessions(self) -> list[Session]:
        """List all sessions, ordered by most recently active first."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM sessions ORDER BY last_heartbeat DESC").fetchall()
            return [self._row_to_session(row) for row in rows]

    def cleanup_stale_sessions(self, timeout_seconds: int = SESSION_TIMEOUT) -> int:
        """Remove sessions that haven't sent a heartbeat recently.

        Returns the number of sessions removed.
        """
        cutoff = datetime.now().timestamp() - timeout_seconds
        cutoff_dt = datetime.fromtimestamp(cutoff)

        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM sessions WHERE last_heartbeat < ?", (cutoff_dt,))
            count = cursor.rowcount
            if count > 0:
                logger.warning(f"Cleaned up {count} stale session(s)")
            return count

    def session_count(self) -> int:
        """Get count of active sessions."""
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) as count FROM sessions").fetchone()
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
    ) -> tuple[list[Event], str | None]:
        """Get events with cursor-based pagination.

        Args:
            cursor: Opaque position from previous call. None = start from recent.
            limit: Maximum number of events to return.
            channels: Optional list of channels to filter by (None = all events).
            order: "desc" (newest first, default) or "asc" (oldest first).

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
                    logger.warning(f"Malformed cursor '{cursor}', resetting to start")
                    since_id = 0

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
                params = (*params_base, *channels, limit)
            else:
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
