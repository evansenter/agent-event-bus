"""MCP Event Bus Server.

Provides tools for cross-session Claude Code communication:
- register_session: Announce session presence
- list_sessions: See active sessions
- publish_event: Broadcast events (auto-refreshes heartbeat)
- get_events: Poll for new events (auto-refreshes heartbeat)
- unregister_session: Clean up on exit
- notify: Send system notifications
"""

import logging
import os
import socket
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastmcp import FastMCP

from event_bus.helpers import (
    dev_notify,
    extract_repo_from_cwd,
    is_client_alive,
    send_notification,
)
from event_bus.middleware import RequestLoggingMiddleware
from event_bus.session_ids import generate_session_id
from event_bus.storage import Session, SQLiteStorage

# Configure logging - only enable DEBUG for our logger, not third-party libs
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("event-bus")
if os.environ.get("DEV_MODE"):
    logger.setLevel(logging.DEBUG)

# Initialize MCP server
mcp = FastMCP("event-bus")

# SQLite-backed storage (persists across restarts)
storage = SQLiteStorage()


@mcp.resource("event-bus://guide", description="Usage guide and best practices")
def usage_guide() -> str:
    """Return the event bus usage guide from external markdown file."""
    guide_path = Path(__file__).parent / "guide.md"
    try:
        return guide_path.read_text()
    except FileNotFoundError:
        return "# Event Bus Usage Guide\n\nGuide file not found. See CLAUDE.md for usage."


def _auto_heartbeat(session_id: str | None) -> None:
    """Refresh heartbeat for a session if it exists."""
    if session_id and session_id != "anonymous":
        storage.update_heartbeat(session_id, datetime.now())


def _notify_dm_recipient(
    channel: str,
    payload: str,
    sender_session_id: str | None,
) -> None:
    """Send a notification to the recipient of a direct message.

    This handles the "human as router" pattern - we notify the human about
    incoming DMs so they can route the message to the correct Claude session.

    Args:
        channel: The target channel (must be "session:<id>" format)
        payload: The message payload (will be truncated for notification)
        sender_session_id: The sender's session ID for attribution
    """
    if not channel.startswith("session:"):
        return

    parts = channel.split(":", 1)
    if len(parts) != 2 or not parts[1]:
        logger.warning(f"Invalid session channel format: '{channel}'. Expected 'session:<id>'")
        return

    target_id = parts[1]
    target_session = storage.get_session(target_id)

    if not target_session:
        logger.warning(
            f"Cannot send DM notification: target session '{target_id}' not found. "
            f"The event was published to channel 'session:{target_id}', but no active session "
            f"exists with that ID. Session may have expired or ID may be incorrect."
        )
        return

    # Get sender info for notification context
    sender_name = "anonymous"
    if sender_session_id:
        sender_session = storage.get_session(sender_session_id)
        if sender_session:
            sender_name = sender_session.name
        else:
            logger.warning(
                f"Sender session '{sender_session_id}' not found when sending DM to {target_id}. "
                f"Using anonymous as sender name."
            )

    # Send notification to alert the human
    payload_preview = payload[:50] + "..." if len(payload) > 50 else payload
    try:
        project_name = target_session.get_project_name()
        notification_sent = send_notification(
            title=f"📨 {target_session.name} • {project_name}",
            message=f"From: {sender_name}\n{payload_preview}",
        )

        if not notification_sent:
            logger.warning(
                f"Failed to send DM notification to session {target_id} "
                f"(name: {target_session.name}, sender: {sender_name}). "
                f"The event was published successfully, but the human may not be notified."
            )
    except Exception as e:
        logger.error(
            f"Exception while sending DM notification to session {target_id}: {e}. "
            f"The event will still be published."
        )


@mcp.tool()
def register_session(
    name: str,
    machine: str | None = None,
    cwd: str | None = None,
    client_id: str | None = None,
) -> dict:
    """Register this Claude session with the event bus.

    Args:
        name: A short name for this session (e.g., branch name, task)
        machine: Machine identifier (defaults to hostname)
        cwd: Working directory (defaults to CWD env var)
        client_id: Client identifier for session deduplication (e.g., CC session ID or PID)

    Returns:
        Session info including assigned session_id and cursor for polling

    Tip: Read the resource at event-bus://guide for usage patterns and best practices.
    """
    storage.cleanup_stale_sessions()

    now = datetime.now()
    machine = machine or socket.gethostname()
    cwd = cwd or os.environ.get("PWD", os.getcwd())
    repo = extract_repo_from_cwd(cwd)

    # Check for existing session with same machine+client_id
    existing = None
    if client_id is not None:
        existing = storage.find_session_by_client(machine, client_id)

    if existing:
        # Update existing session
        existing.name = name
        existing.last_heartbeat = now
        storage.add_session(existing)  # INSERT OR REPLACE
        dev_notify("register_session", f"{name} resumed → {existing.id}")
        return {
            "session_id": existing.id,
            "name": name,
            "machine": machine,
            "cwd": cwd,
            "repo": repo,
            "active_sessions": storage.session_count(),
            "cursor": storage.get_cursor(),
            "resumed": True,
            "tip": f"You are '{name}' ({existing.id}). Use cursor to start polling: get_events(cursor=cursor).",
        }

    # Create new session with human-readable ID
    session_id = generate_session_id()
    session = Session(
        id=session_id,
        name=name,
        machine=machine,
        cwd=cwd,
        repo=repo,
        registered_at=now,
        last_heartbeat=now,
        client_id=client_id,
    )
    storage.add_session(session)

    # Auto-publish registration event and capture its ID directly
    # (avoids race condition if another event is published between add and get)
    registration_event = storage.add_event(
        event_type="session_registered",
        payload=f"{name} started on {machine} in {cwd}",
        session_id=session_id,
    )

    result = {
        "session_id": session_id,
        "name": name,
        "machine": machine,
        "cwd": cwd,
        "repo": repo,
        "active_sessions": storage.session_count(),
        "cursor": str(registration_event.id),
        "resumed": False,
        "tip": f"You are '{name}' ({session_id}). Use cursor to start polling: get_events(cursor=cursor).",
    }
    dev_notify("register_session", f"{name} → {session_id}")
    return result


@mcp.tool()
def list_sessions() -> list[dict]:
    """List all active sessions on the event bus.

    Sessions are ordered by most recently active first (last_heartbeat DESC),
    so the most likely-to-be-alive sessions appear first.

    Returns:
        List of active sessions with their info
    """
    storage.cleanup_stale_sessions()

    local_hostname = socket.gethostname()
    results = []

    for s in storage.list_sessions():
        # For local sessions, check if client is still alive
        is_local = s.machine == local_hostname
        client_alive = is_client_alive(s.client_id, is_local)

        if not client_alive:
            # Clean up dead session
            storage.delete_session(s.id)
            logger.info(f"Cleaned up dead session {s.id} (client_id {s.client_id} not running)")
            continue

        results.append(
            {
                "session_id": s.id,
                "name": s.name,
                "machine": s.machine,
                "repo": s.repo,
                "cwd": s.cwd,
                "client_id": s.client_id,
                "registered_at": s.registered_at.isoformat(),
                "last_heartbeat": s.last_heartbeat.isoformat(),
                "age_seconds": (datetime.now() - s.registered_at).total_seconds(),
            }
        )

    dev_notify("list_sessions", f"{len(results)} active")
    return results


@mcp.tool()
def publish_event(
    event_type: str,
    payload: str,
    session_id: str | None = None,
    channel: str = "all",
) -> dict:
    """Publish an event to a channel.

    Args:
        event_type: Type of event (e.g., 'task_completed', 'help_needed')
        payload: Event payload/message
        session_id: Your session ID (for attribution and auto-heartbeat)
        channel: Target channel (default: "all" for broadcast)
            - "all": Broadcast to everyone
            - "session:{id}": Direct message to specific session
            - "repo:{name}": All sessions in that repo
            - "machine:{name}": All sessions on that machine

    Returns:
        The created event with its ID
    """
    # Auto-refresh heartbeat when session publishes
    _auto_heartbeat(session_id)

    # Validate channel format for known channel types
    if channel not in ["all"] and ":" in channel:
        channel_type, _, channel_value = channel.partition(":")
        if channel_type in ["session", "repo", "machine"]:
            if not channel_value:
                logger.warning(
                    f"Invalid {channel_type} channel format: '{channel}'. "
                    f"Expected '{channel_type}:<value>'"
                )

    # Auto-notify on direct messages (DMs)
    _notify_dm_recipient(channel, payload, session_id)

    event = storage.add_event(
        event_type=event_type,
        payload=payload,
        session_id=session_id or "anonymous",
        channel=channel,
    )

    truncated = payload[:50] + "..." if len(payload) > 50 else payload
    dev_notify("publish_event", f"{event_type} [{channel}] {truncated}")

    return {
        "event_id": event.id,
        "event_type": event_type,
        "payload": payload,
        "channel": channel,
    }


def _get_implicit_channels(session_id: str | None) -> list[str] | None:
    """Get the channels a session is implicitly subscribed to.

    Returns None if no session (returns all events), or a list of channels.
    """
    if not session_id:
        return None  # No filtering, return all events

    session = storage.get_session(session_id)
    if not session:
        return None  # Session not found, return all events

    # Implicit subscriptions based on session attributes
    return [
        "all",  # Broadcasts
        f"session:{session_id}",  # Direct messages to this session
        f"repo:{session.repo}",  # Same repo
        f"machine:{session.machine}",  # Same machine
    ]


@mcp.tool()
def get_events(
    cursor: str | None = None,
    limit: int = 50,
    session_id: str | None = None,
    order: Literal["asc", "desc"] = "desc",
) -> dict:
    """Get events from the event bus.

    Args:
        cursor: Position from previous call or register_session. None = recent activity.
        limit: Maximum number of events to return (default: 50).
        session_id: Your session ID (for auto-heartbeat and channel filtering).
        order: "desc" (newest first, default) or "asc" (oldest first).

    Returns:
        Dict with "events" list and "next_cursor" for pagination.

    Typical usage:
    1. On session start, get cursor from register_session()
    2. Poll with get_events(cursor=cursor) to get events
    3. Use next_cursor from response for subsequent calls
    4. Use get_events() (no cursor) to see recent activity

    Events are filtered to channels the session is subscribed to:
    - "all": Broadcasts (everyone receives)
    - "session:{your_id}": Direct messages to you
    - "repo:{your_repo}": Events for your repo
    - "machine:{your_machine}": Events for your machine
    """
    # Auto-refresh heartbeat when session polls
    _auto_heartbeat(session_id)

    storage.cleanup_stale_sessions()

    # Get channels this session is subscribed to
    channels = _get_implicit_channels(session_id)

    raw_events, next_cursor = storage.get_events(
        cursor=cursor, limit=limit, channels=channels, order=order
    )

    events = [
        {
            "id": e.id,
            "event_type": e.event_type,
            "payload": e.payload,
            "session_id": e.session_id,
            "timestamp": e.timestamp.isoformat(),
            "channel": e.channel,
        }
        for e in raw_events
    ]

    dev_notify("get_events", f"{len(events)} events (cursor={cursor})")

    return {
        "events": events,
        "next_cursor": next_cursor,
    }


@mcp.tool()
def unregister_session(session_id: str) -> dict:
    """Unregister a session from the event bus.

    Call this when a Claude session is ending to clean up immediately
    rather than waiting for heartbeat timeout.

    Args:
        session_id: Your session ID from register_session

    Returns:
        Success status
    """
    session = storage.get_session(session_id)
    if not session:
        dev_notify("unregister_session", f"{session_id} not found")
        return {"error": "Session not found", "session_id": session_id}

    storage.delete_session(session_id)

    # Publish unregister event
    storage.add_event(
        event_type="session_unregistered",
        payload=f"{session.name} ended on {session.machine}",
        session_id=session_id,
    )

    dev_notify("unregister_session", f"{session.name} ({session_id})")
    return {
        "success": True,
        "session_id": session_id,
        "active_sessions": storage.session_count(),
    }


@mcp.tool()
def notify(title: str, message: str, sound: bool = False) -> dict:
    """Send a system notification to the user.

    Use this to alert the user about important events like task completion,
    errors, or when help is needed.

    Args:
        title: Notification title (short, e.g., "Build Complete")
        message: Notification body (details)
        sound: Whether to play a sound (default: False)

    Returns:
        Success status
    """
    success = send_notification(title, message, sound)
    return {
        "success": success,
        "title": title,
        "message": message,
    }


def create_app():
    """Create the ASGI app with optional logging middleware."""
    # stateless_http=True allows resilience to server restarts
    app = mcp.http_app(stateless_http=True)

    if os.environ.get("DEV_MODE"):
        logger.info("Dev mode enabled - logging all requests")
        return RequestLoggingMiddleware(app)

    return app


def main():
    """Run the MCP server."""
    import uvicorn

    port = int(os.environ.get("PORT", 8080))
    host = os.environ.get("HOST", "127.0.0.1")

    print(f"Starting Claude Event Bus on {host}:{port}")
    print(
        f"Add to Claude Code: claude mcp add --transport http --scope user event-bus http://{host}:{port}/mcp"
    )

    # FastMCP provides an ASGI app (wrap with logging in dev mode)
    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    main()
