"""SQLite storage backend for event bus persistence."""

import logging
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

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
    pid: int | None = None  # Client process ID for deduplication

    def get_project_name(self) -> str:
        """Get the project name, preferring explicit repo over cwd basename.

        Returns:
            Project name derived from repo field, or the last directory component of cwd
        """
        if self.repo:
            return self._sanitize_project_name(self.repo)

        if self.cwd:
            # Strip trailing slashes to handle paths like "/path/to/project/"
            basename = os.path.basename(self.cwd.rstrip("/"))
            if basename:
                return self._sanitize_project_name(basename)

        return "unknown"

    def _sanitize_project_name(self, name: str) -> str:
        """Sanitize project name by replacing special characters."""
        return name.replace("\n", " ").replace("\t", " ").replace("\r", " ")


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

# Session timeout in seconds (7 days without activity = dead)
# Long timeout supports multi-day sessions; local crashed sessions are
# cleaned up faster via PID liveness check in list_sessions()
SESSION_TIMEOUT = 604800  # 7 days

# Event retention settings
MAX_EVENTS = 1000  # Keep last N events


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

    def _init_db(self):
        """Create tables if they don't exist."""
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
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
            # Add pid column if upgrading from older schema
            try:
                conn.execute("ALTER TABLE sessions ADD COLUMN pid INTEGER")
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

    # Session operations

    def add_session(self, session: Session) -> None:
        """Add or update a session."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO sessions
                (id, name, machine, cwd, repo, registered_at, last_heartbeat, pid)
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
                    session.pid,
                ),
            )

    def find_session_by_key(self, machine: str, cwd: str, pid: int) -> Session | None:
        """Find an existing session by machine+cwd+pid key."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE machine = ? AND cwd = ? AND pid = ?",
                (machine, cwd, pid),
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
            pid=row["pid"],
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
        """List all sessions (including stale ones)."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM sessions").fetchall()
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

            # Cleanup old events
            self._cleanup_events(conn)

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
        since_id: int = 0,
        limit: int = 50,
        channels: list[str] | None = None,
    ) -> list[Event]:
        """Get events since a given event ID.

        Args:
            since_id: Return events with ID greater than this
            limit: Maximum number of events to return
            channels: Optional list of channels to filter by (None = all events)
        """
        with self._connect() as conn:
            if channels:
                # Filter by channels
                placeholders = ",".join("?" * len(channels))
                rows = conn.execute(
                    f"""
                    SELECT * FROM events
                    WHERE id > ? AND channel IN ({placeholders})
                    ORDER BY id ASC
                    LIMIT ?
                    """,
                    (since_id, *channels, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM events
                    WHERE id > ?
                    ORDER BY id ASC
                    LIMIT ?
                    """,
                    (since_id, limit),
                ).fetchall()

            return [
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

    def _cleanup_events(self, conn: sqlite3.Connection) -> int:
        """Remove old events, keeping only the last MAX_EVENTS.

        Returns the number of events removed.
        """
        cursor = conn.execute(
            """
            DELETE FROM events
            WHERE id NOT IN (
                SELECT id FROM events ORDER BY id DESC LIMIT ?
            )
            """,
            (MAX_EVENTS,),
        )
        count = cursor.rowcount
        if count > 0:
            logger.warning(f"Cleaned up {count} old event(s)")
        return count

    def get_last_event_id(self) -> int:
        """Get the ID of the most recent event, or 0 if none."""
        with self._connect() as conn:
            row = conn.execute("SELECT MAX(id) as max_id FROM events").fetchone()
            return row["max_id"] or 0
