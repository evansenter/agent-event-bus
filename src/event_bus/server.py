"""MCP Event Bus Server.

Provides tools for cross-session Claude Code communication:
- register_session: Announce session presence
- list_sessions: See active sessions
- publish_event: Broadcast events
- get_events: Poll for new events
- heartbeat: Keep session alive
"""

import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from fastmcp import FastMCP

# Initialize MCP server
mcp = FastMCP("event-bus")


# In-memory storage (MVP - will add persistence later)
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


@dataclass
class Event:
    """An event broadcast to all sessions."""

    id: int
    event_type: str
    payload: str
    session_id: str
    timestamp: datetime


# Global state
sessions: dict[str, Session] = {}
events: list[Event] = []
event_counter: int = 0

# Session timeout (seconds)
SESSION_TIMEOUT = 120  # 2 minutes without heartbeat = dead


def _extract_repo_from_cwd(cwd: str) -> str:
    """Extract repo name from working directory."""
    # Try to get git repo name
    parts = cwd.rstrip("/").split("/")
    # Look for common patterns like .worktrees/branch-name
    if ".worktrees" in parts:
        idx = parts.index(".worktrees")
        if idx > 0:
            return parts[idx - 1]
    # Fall back to last directory component
    return parts[-1] if parts else "unknown"


def _cleanup_stale_sessions() -> None:
    """Remove sessions that haven't sent a heartbeat recently."""
    now = datetime.now()
    stale = [
        sid
        for sid, session in sessions.items()
        if (now - session.last_heartbeat).total_seconds() > SESSION_TIMEOUT
    ]
    for sid in stale:
        del sessions[sid]


@mcp.tool()
def register_session(
    name: str, machine: Optional[str] = None, cwd: Optional[str] = None
) -> dict:
    """Register this Claude session with the event bus.

    Args:
        name: A short name for this session (e.g., branch name, task)
        machine: Machine identifier (defaults to hostname)
        cwd: Working directory (defaults to CWD env var)

    Returns:
        Session info including assigned session_id
    """
    global event_counter

    _cleanup_stale_sessions()

    session_id = str(uuid.uuid4())[:8]
    now = datetime.now()

    machine = machine or os.environ.get("HOSTNAME", "unknown")
    cwd = cwd or os.environ.get("PWD", os.getcwd())
    repo = _extract_repo_from_cwd(cwd)

    session = Session(
        id=session_id,
        name=name,
        machine=machine,
        cwd=cwd,
        repo=repo,
        registered_at=now,
        last_heartbeat=now,
    )
    sessions[session_id] = session

    # Auto-publish registration event
    event_counter += 1
    events.append(
        Event(
            id=event_counter,
            event_type="session_registered",
            payload=f"{name} started on {machine} in {cwd}",
            session_id=session_id,
            timestamp=now,
        )
    )

    return {
        "session_id": session_id,
        "name": name,
        "machine": machine,
        "cwd": cwd,
        "repo": repo,
        "active_sessions": len(sessions),
    }


@mcp.tool()
def list_sessions() -> list[dict]:
    """List all active sessions on the event bus.

    Returns:
        List of active sessions with their info
    """
    _cleanup_stale_sessions()

    return [
        {
            "session_id": s.id,
            "name": s.name,
            "machine": s.machine,
            "repo": s.repo,
            "cwd": s.cwd,
            "registered_at": s.registered_at.isoformat(),
            "last_heartbeat": s.last_heartbeat.isoformat(),
            "age_seconds": (datetime.now() - s.registered_at).total_seconds(),
        }
        for s in sessions.values()
    ]


@mcp.tool()
def publish_event(
    event_type: str, payload: str, session_id: Optional[str] = None
) -> dict:
    """Publish an event to all sessions.

    Args:
        event_type: Type of event (e.g., 'task_completed', 'help_needed')
        payload: Event payload/message
        session_id: Your session ID (for attribution)

    Returns:
        The created event with its ID
    """
    global event_counter
    event_counter += 1

    event = Event(
        id=event_counter,
        event_type=event_type,
        payload=payload,
        session_id=session_id or "anonymous",
        timestamp=datetime.now(),
    )
    events.append(event)

    return {
        "event_id": event.id,
        "event_type": event_type,
        "payload": payload,
    }


@mcp.tool()
def get_events(since_id: int = 0, limit: int = 50) -> list[dict]:
    """Get events since a given event ID.

    Args:
        since_id: Return events with ID greater than this (default: 0 = all)
        limit: Maximum number of events to return (default: 50)

    Returns:
        List of events since the given ID
    """
    filtered = [e for e in events if e.id > since_id]
    filtered = filtered[-limit:]  # Take last N

    return [
        {
            "id": e.id,
            "event_type": e.event_type,
            "payload": e.payload,
            "session_id": e.session_id,
            "timestamp": e.timestamp.isoformat(),
        }
        for e in filtered
    ]


@mcp.tool()
def heartbeat(session_id: str) -> dict:
    """Send a heartbeat to keep session alive.

    Args:
        session_id: Your session ID from register_session

    Returns:
        Updated session info
    """
    if session_id not in sessions:
        return {"error": "Session not found", "session_id": session_id}

    session = sessions[session_id]
    session.last_heartbeat = datetime.now()

    return {
        "session_id": session_id,
        "last_heartbeat": session.last_heartbeat.isoformat(),
        "active_sessions": len(sessions),
    }


def main():
    """Run the MCP server."""
    import uvicorn

    port = int(os.environ.get("PORT", 8080))
    host = os.environ.get("HOST", "127.0.0.1")

    print(f"Starting Claude Event Bus on {host}:{port}")
    print(
        f"Add to Claude Code: claude mcp add --transport http --scope user event-bus http://{host}:{port}/mcp"
    )

    # FastMCP provides an ASGI app
    uvicorn.run(mcp.get_app(), host=host, port=port)


if __name__ == "__main__":
    main()
