"""SQLite storage backend for event bus persistence."""

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


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
    pid: Optional[int] = None  # Client process ID for deduplication


@dataclass
class Event:
    """An event broadcast to all sessions."""

    id: int
    event_type: str
    payload: str
    session_id: str
    timestamp: datetime


# Default database path
DEFAULT_DB_PATH = Path.home() / ".claude" / "event-bus.db"

# Session timeout in seconds (2 minutes without heartbeat = dead)
SESSION_TIMEOUT = 120

# Event retention settings
MAX_EVENTS = 1000  # Keep last N events


class SQLiteStorage:
    """SQLite-backed storage for sessions and events."""

    def __init__(self, db_path: Optional[str] = None):
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
                    timestamp TIMESTAMP NOT NULL
                )
            """)
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
                (id, name, machine, cwd, repo, registered_at, last_heartbeat)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.id,
                    session.name,
                    session.machine,
                    session.cwd,
                    session.repo,
                    session.registered_at,
                    session.last_heartbeat,
                ),
            )

    def get_session(self, session_id: str) -> Optional[Session]:
        """Get a session by ID."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row:
                return Session(
                    id=row["id"],
                    name=row["name"],
                    machine=row["machine"],
                    cwd=row["cwd"],
                    repo=row["repo"],
                    registered_at=row["registered_at"],
                    last_heartbeat=row["last_heartbeat"],
                )
            return None

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
            return [
                Session(
                    id=row["id"],
                    name=row["name"],
                    machine=row["machine"],
                    cwd=row["cwd"],
                    repo=row["repo"],
                    registered_at=row["registered_at"],
                    last_heartbeat=row["last_heartbeat"],
                )
                for row in rows
            ]

    def cleanup_stale_sessions(self, timeout_seconds: int = SESSION_TIMEOUT) -> int:
        """Remove sessions that haven't sent a heartbeat recently.

        Returns the number of sessions removed.
        """
        cutoff = datetime.now().timestamp() - timeout_seconds
        cutoff_dt = datetime.fromtimestamp(cutoff)

        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM sessions WHERE last_heartbeat < ?", (cutoff_dt,)
            )
            return cursor.rowcount

    def session_count(self) -> int:
        """Get count of active sessions."""
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) as count FROM sessions").fetchone()
            return row["count"]

    # Event operations

    def add_event(self, event_type: str, payload: str, session_id: str) -> Event:
        """Add a new event and return it with assigned ID."""
        now = datetime.now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO events (event_type, payload, session_id, timestamp)
                VALUES (?, ?, ?, ?)
                """,
                (event_type, payload, session_id, now),
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
            )

    def get_events(self, since_id: int = 0, limit: int = 50) -> list[Event]:
        """Get events since a given event ID."""
        with self._connect() as conn:
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
                )
                for row in rows
            ]

    def _cleanup_events(self, conn: sqlite3.Connection) -> None:
        """Remove old events, keeping only the last MAX_EVENTS."""
        conn.execute(
            """
            DELETE FROM events
            WHERE id NOT IN (
                SELECT id FROM events ORDER BY id DESC LIMIT ?
            )
            """,
            (MAX_EVENTS,),
        )

    def get_last_event_id(self) -> int:
        """Get the ID of the most recent event, or 0 if none."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(id) as max_id FROM events"
            ).fetchone()
            return row["max_id"] or 0
