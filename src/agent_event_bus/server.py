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
import uuid
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastmcp import FastMCP

from agent_event_bus.helpers import (
    _dev_notify,
    extract_repo_from_cwd,
    is_client_alive,
    send_notification,
)
from agent_event_bus.middleware import RequestLoggingMiddleware, TailscaleAuthMiddleware
from agent_event_bus.session_ids import generate_session_id
from agent_event_bus.storage import Session, SQLiteStorage

# Configure logging
# Always log to ~/.claude/contrib/agent-event-bus/agent-event-bus.log for tail -f access
# In dev mode, also log to console
# Skip file logging during tests to avoid polluting production logs
LOG_FILE = Path.home() / ".claude" / "contrib" / "agent-event-bus" / "agent-event-bus.log"

logger = logging.getLogger("agent-event-bus")
logger.setLevel(logging.DEBUG if os.environ.get("DEV_MODE") else logging.INFO)

# File handler - skip during tests, guard against reimport duplication
# Check both PYTEST_CURRENT_TEST (set per-test) and AGENT_EVENT_BUS_TESTING (set in conftest.py)
if (
    not os.environ.get("PYTEST_CURRENT_TEST")
    and not os.environ.get("AGENT_EVENT_BUS_TESTING")
    and not logger.handlers
):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(LOG_FILE)
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter("%(asctime)s │ %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(file_handler)

    # Console handler - only in dev mode
    if os.environ.get("DEV_MODE"):
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG)
        console_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
        )
        logger.addHandler(console_handler)

# Constants
MAX_PAYLOAD_PREVIEW = 50  # Max chars to show in notification previews

# Initialize MCP server
mcp = FastMCP("agent-event-bus")

# SQLite-backed storage (persists across restarts)
storage = SQLiteStorage()


@mcp.resource("agent-event-bus://guide", description="Usage guide and best practices")
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


def _get_session_channels(session: Session) -> list[str]:
    """Compute implicit channel subscriptions for a session.

    Sessions are auto-subscribed to channels based on their attributes.
    """
    return [
        "all",  # Broadcasts
        f"session:{session.id}",  # Direct messages to this session
        f"repo:{session.repo}",  # Same repo
        f"machine:{session.machine}",  # Same machine
    ]


def _get_live_sessions() -> list[Session]:
    """Get live sessions, cleaning up dead ones.

    For local sessions, checks if the client process is still alive.
    Remote sessions and sessions without client_id are assumed alive.

    Returns:
        List of sessions that are still alive
    """
    storage.cleanup_stale_sessions()
    local_hostname = socket.gethostname()
    live = []

    for s in storage.list_sessions():
        is_local = s.machine == local_hostname
        if not is_client_alive(s.client_id, is_local):
            storage.delete_session(s.id)
            continue
        live.append(s)

    return live


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
        return  # Invalid format, silently skip

    target_id = parts[1]
    target_session = storage.get_session(target_id)

    if not target_session:
        return  # Session not found, silently skip

    # Get sender info for notification context
    sender_name = "anonymous"
    if sender_session_id:
        sender_session = storage.get_session(sender_session_id)
        if sender_session:
            sender_name = sender_session.name
        # If sender not found, keep "anonymous" - don't log (normal during tests/cleanup)

    # Send notification to alert the human
    payload_preview = (
        payload[:MAX_PAYLOAD_PREVIEW] + "..." if len(payload) > MAX_PAYLOAD_PREVIEW else payload
    )
    try:
        project_name = target_session.get_project_name()
        send_notification(
            title=f"📨 {target_session.name} • {project_name}",
            message=f"From: {sender_name}\n{payload_preview}",
        )
    except Exception as e:
        # Notification failure is non-critical, but log for debugging
        logger.warning(f"Failed to notify session {target_id} of DM: {e}")


@mcp.tool()
def register_session(
    name: str,
    machine: str | None = None,
    cwd: str | None = None,
    client_id: str | None = None,
) -> dict:
    """Register with the event bus.

    Args:
        name: Session name (e.g., branch name, task)
        machine: Defaults to hostname
        cwd: Defaults to $PWD
        client_id: Enables session resumption via (machine, client_id)
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
        _dev_notify("register_session", f"{name} resumed → {existing.display_id}")

        # Use session's last_cursor if available (resume where they left off)
        # Otherwise fall back to current position
        resume_cursor = existing.last_cursor or storage.get_cursor()
        return {
            "session_id": existing.id,
            "display_id": existing.display_id,
            "name": name,
            "machine": machine,
            "cwd": cwd,
            "repo": repo,
            "active_sessions": storage.session_count(),
            "cursor": resume_cursor,
            "resumed": True,
            "tip": f"You are '{name}' ({existing.display_id}). Resuming from last seen cursor.",
        }

    # Create new session
    # Use client_id as session ID if provided (allows direct lookup by CC's session_id)
    # Otherwise generate a UUID for new sessions without client_id
    session_id = client_id if client_id else str(uuid.uuid4())
    # Always generate human-readable display_id for UI/logs
    display_id = generate_session_id()
    session = Session(
        id=session_id,
        display_id=display_id,
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
        "display_id": display_id,
        "name": name,
        "machine": machine,
        "cwd": cwd,
        "repo": repo,
        "active_sessions": storage.session_count(),
        "cursor": str(registration_event.id),
        "resumed": False,
        "tip": f"You are '{name}' ({display_id}). Use cursor to start polling: get_events(cursor=cursor).",
    }
    _dev_notify("register_session", f"{name} → {display_id}")
    return result


@mcp.tool()
def list_sessions() -> list[dict]:
    """List active sessions, ordered by most recently active."""
    results = []

    for s in _get_live_sessions():
        results.append(
            {
                "session_id": s.id,
                "display_id": s.display_id,
                "name": s.name,
                "machine": s.machine,
                "repo": s.repo,
                "cwd": s.cwd,
                "client_id": s.client_id,
                "registered_at": s.registered_at.isoformat(),
                "last_heartbeat": s.last_heartbeat.isoformat(),
                "age_seconds": (datetime.now() - s.registered_at).total_seconds(),
                "subscribed_channels": _get_session_channels(s),
            }
        )

    _dev_notify("list_sessions", f"{len(results)} active")
    return results


@mcp.tool()
def list_channels() -> list[dict]:
    """List channels with subscriber counts."""
    channel_subscribers: dict[str, int] = {}

    for s in _get_live_sessions():
        for ch in _get_session_channels(s):
            channel_subscribers[ch] = channel_subscribers.get(ch, 0) + 1

    # Build result - only channels with >0 subscribers (all of them at this point)
    results = [
        {"channel": ch, "subscribers": count} for ch, count in sorted(channel_subscribers.items())
    ]

    _dev_notify("list_channels", f"{len(results)} active channels")
    return results


@mcp.tool()
def publish_event(
    event_type: str,
    payload: str,
    session_id: str | None = None,
    channel: str = "all",
) -> dict:
    """Publish an event. Auto-refreshes heartbeat.

    Args:
        event_type: e.g., 'task_completed', 'help_needed'
        payload: Event message
        session_id: Your session ID
        channel: "all", "session:{id}", "repo:{name}", or "machine:{name}"
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

    truncated = (
        payload[:MAX_PAYLOAD_PREVIEW] + "..." if len(payload) > MAX_PAYLOAD_PREVIEW else payload
    )
    _dev_notify("publish_event", f"{event_type} [{channel}] {truncated}")

    return {
        "event_id": event.id,
        "event_type": event_type,
        "payload": payload,
        "channel": channel,
    }


def _get_implicit_channels(session_id: str | None) -> list[str] | None:
    """Get the channels a session is implicitly subscribed to.

    Returns None to disable filtering - all sessions see all events (broadcast model).
    Channel metadata is preserved on events for informational purposes.
    """
    # Broadcast model: everyone sees everything
    # Explicit channel filtering via get_events(channel=X) still works if needed
    return None


@mcp.tool()
def get_events(
    cursor: str | None = None,
    limit: int = 50,
    session_id: str | None = None,
    order: Literal["asc", "desc"] = "desc",
    channel: str | None = None,
    resume: bool = False,
    event_types: list[str] | None = None,
) -> dict:
    """Get events. Auto-refreshes heartbeat.

    Args:
        cursor: Position from register_session or previous call
        limit: Max events (default: 50)
        session_id: Enables cursor auto-tracking
        order: "desc" (newest first) or "asc"
        channel: Filter to specific channel
        resume: Use saved cursor (requires session_id)
        event_types: Filter by types, e.g., ["task_completed"]
    """
    # Auto-refresh heartbeat when session polls
    _auto_heartbeat(session_id)

    # Resume from saved cursor if requested
    # Only applies when: resume=True, session_id provided, cursor not provided
    if resume and session_id and cursor is None:
        session = storage.get_session(session_id)
        if session and session.last_cursor:
            cursor = session.last_cursor

    storage.cleanup_stale_sessions()

    # Determine channel filtering:
    # - If explicit channel provided, filter to that channel
    # - Otherwise, return all events (broadcast model)
    if channel:
        channels = [channel]
    else:
        channels = _get_implicit_channels(session_id)

    raw_events, next_cursor = storage.get_events(
        cursor=cursor, limit=limit, channels=channels, order=order, event_types=event_types
    )

    # Persist high-water mark for session-based tracking (enables seamless resume)
    # We save the MAX event ID seen, not the pagination cursor. This ensures that
    # resume=True always starts from after the newest event seen, regardless of
    # what order was used for polling.
    # Note: Updates on any poll - any poll means the session has "seen" events up to this point.
    # Silently ignore unknown session_ids - callers may pass external session IDs
    # (like Claude Code's own UUIDs) that aren't registered with us.
    if session_id and raw_events:
        high_water_mark = str(max(e.id for e in raw_events))
        storage.update_session_cursor(session_id, high_water_mark)

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

    _dev_notify("get_events", f"{len(events)} events (cursor={cursor})")

    return {
        "events": events,
        "next_cursor": next_cursor,
    }


@mcp.tool()
def unregister_session(session_id: str | None = None, client_id: str | None = None) -> dict:
    """Unregister from event bus. session_id takes precedence if both given.

    Args:
        session_id: Your session ID
        client_id: Alternative - looks up by (machine, client_id)
    """
    # Look up session by client_id if provided
    if client_id and not session_id:
        machine = socket.gethostname()
        session = storage.find_session_by_client(machine, client_id)
        if session:
            session_id = session.id
        else:
            _dev_notify("unregister_session", f"client_id {client_id} not found")
            return {"error": "Session not found", "client_id": client_id, "machine": machine}
    elif not session_id:
        _dev_notify("unregister_session", "no identifier provided")
        return {"error": "Must provide either session_id or client_id"}

    session = storage.get_session(session_id)
    if not session:
        _dev_notify("unregister_session", f"{session_id} not found")
        return {"error": "Session not found", "session_id": session_id}

    storage.delete_session(session_id)

    # Publish unregister event
    storage.add_event(
        event_type="session_unregistered",
        payload=f"{session.name} ended on {session.machine}",
        session_id=session_id,
    )

    _dev_notify("unregister_session", f"{session.name} ({session.display_id})")
    return {
        "success": True,
        "session_id": session_id,
        "display_id": session.display_id,
        "active_sessions": storage.session_count(),
    }


@mcp.tool()
def notify(title: str, message: str, sound: bool = False) -> dict:
    """Send a system notification.

    Args:
        title: Short title
        message: Body text
        sound: Play sound (default: False)
    """
    success = send_notification(title, message, sound)
    return {
        "success": success,
        "title": title,
        "message": message,
    }


def create_app():
    """Create the ASGI app with middleware stack.

    Middleware order (outer to inner):
    1. TailscaleAuthMiddleware - requires Tailscale identity headers
    2. RequestLoggingMiddleware - logs MCP tool calls

    All MCP tool calls are logged to ~/.claude/contrib/agent-event-bus/agent-event-bus.log.
    Use `tail -f ~/.claude/contrib/agent-event-bus/agent-event-bus.log` to watch activity.

    Set AGENT_EVENT_BUS_AUTH_DISABLED=1 to disable auth (for testing/local dev).
    """
    # stateless_http=True allows resilience to server restarts
    app = mcp.http_app(stateless_http=True)

    # Always wrap with logging middleware
    app = RequestLoggingMiddleware(app)

    # Wrap with auth middleware unless disabled
    auth_disabled = os.environ.get("AGENT_EVENT_BUS_AUTH_DISABLED", "").lower() in ("1", "true")
    if not auth_disabled:
        app = TailscaleAuthMiddleware(app)
        logger.info("Tailscale auth enabled - requests require identity headers")
    else:
        logger.warning("Tailscale auth DISABLED - all requests allowed")

    return app


def main():
    """Run the MCP server."""
    import uvicorn

    port = int(os.environ.get("PORT", 8080))
    host = os.environ.get("HOST", "127.0.0.1")

    logger.info(f"Starting Agent Event Bus on {host}:{port}")
    print(f"Starting Agent Event Bus on {host}:{port}")
    print(
        f"Add to Claude Code: claude mcp add --transport http --scope user agent-event-bus http://{host}:{port}/mcp"
    )

    # Disable uvicorn's access log - we have our own middleware logging
    # This keeps ~/.claude/contrib/agent-event-bus/agent-event-bus.log clean with just our pretty-printed tool calls
    uvicorn.run(create_app(), host=host, port=port, access_log=False)


if __name__ == "__main__":
    main()
