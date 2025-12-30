"""MCP Event Bus Server.

Provides tools for cross-session Claude Code communication:
- register_session: Announce session presence
- list_sessions: See active sessions
- publish_event: Broadcast events
- get_events: Poll for new events
- heartbeat: Keep session alive
"""

import json
import logging
import os
import platform
import random
import shutil
import socket
import subprocess
from datetime import datetime

from fastmcp import FastMCP

from event_bus.storage import Session, SQLiteStorage

# Word lists for human-readable session IDs (Docker-style)
ADJECTIVES = [
    "brave",
    "calm",
    "clever",
    "eager",
    "fancy",
    "gentle",
    "happy",
    "jolly",
    "keen",
    "lively",
    "merry",
    "nice",
    "polite",
    "quick",
    "sharp",
    "swift",
    "tender",
    "upbeat",
    "vivid",
    "warm",
    "witty",
    "zesty",
    "bold",
    "bright",
    "crisp",
    "daring",
    "epic",
    "fresh",
    "grand",
    "humble",
    "jovial",
    "kind",
]
ANIMALS = [
    "badger",
    "cat",
    "dog",
    "eagle",
    "falcon",
    "gopher",
    "heron",
    "ibis",
    "jaguar",
    "koala",
    "lemur",
    "moose",
    "newt",
    "otter",
    "panda",
    "quail",
    "rabbit",
    "salmon",
    "tiger",
    "urchin",
    "viper",
    "walrus",
    "yak",
    "zebra",
    "bear",
    "crane",
    "duck",
    "fox",
    "goose",
    "hawk",
    "iguana",
    "jay",
]


def _generate_session_id() -> str:
    """Generate a human-readable session ID like 'brave-tiger'."""
    return f"{random.choice(ADJECTIVES)}-{random.choice(ANIMALS)}"


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
    """Return the event bus usage guide."""
    return """# Event Bus Usage Guide

## What is this?

The Event Bus enables communication between Claude Code sessions. When you're
running multiple CC sessions (e.g., via `/parallel-work` or separate terminals),
each session is isolated. This MCP server lets sessions:

- See what other sessions are active
- Coordinate work (signal when APIs are ready, request help)
- Send system notifications to the user

## Available Tools

| Tool | Purpose |
|------|---------|
| `register_session(name, pid?)` | Register yourself, get a session_id |
| `list_sessions()` | See all active sessions |
| `publish_event(type, payload, channel?)` | Send event to a channel |
| `get_events(since_id?, session_id?)` | Poll for new events |
| `unregister_session(session_id)` | Clean up when exiting |
| `notify(title, message, sound?)` | Send macOS notification to user |

## Quick Start

### 1. Register on startup
```
register_session(name="auth-feature")
→ {session_id: "brave-tiger", repo: "my-project", machine: "macbook", ...}
```
Save the `session_id` - you'll need it for other calls.

### 2. Check who else is working
```
list_sessions()
→ [{name: "auth-feature", repo: "my-project"}, {name: "api-refactor", ...}]
```

### 3. Publish events to coordinate
```
publish_event("api_ready", "Auth endpoints merged", channel="repo:my-project")
```

### 4. Poll for events from others
```
get_events(since_id=0, session_id="brave-tiger")
→ [{event_type: "api_ready", payload: "Auth endpoints merged", ...}]
```

### 5. Notify the user
```
notify("Build Complete", "All tests passing", sound=True)
```

### 6. Unregister when done
```
unregister_session(session_id="brave-tiger")
```

## Channels

Events are published to channels. Sessions auto-subscribe based on their attributes:

| Channel | Who Receives | When to Use |
|---------|--------------|-------------|
| `all` | Everyone | Rare - major announcements only |
| `repo:{name}` | Same repository | **Most common** - coordinate parallel work |
| `session:{id}` | One session | Direct messages, help requests |
| `machine:{name}` | Same machine | Local coordination |

**Default is `all`**, but prefer `repo:` for most coordination to avoid noise.

## Common Patterns

### Signal when your work is ready for others
```
# You finished the API, another session is waiting to integrate
publish_event("api_ready", "Auth API merged to main", channel="repo:my-project")
```

### Wait for another session's work
```
# Poll periodically until you see what you're waiting for
events = get_events(since_id=last_seen_id, session_id=my_session_id)
for e in events:
    if e["event_type"] == "api_ready":
        # Now safe to integrate
```

### Ask another session for help
```
# Find the session working on auth
sessions = list_sessions()
auth_session = next(s for s in sessions if "auth" in s["name"])

# Send them a direct message
publish_event("help_needed", "How do I call the new auth endpoint?",
              channel=f"session:{auth_session['session_id']}")
```

### Notify user when task completes
```
# Useful for long-running tasks
notify("PR Created", "https://github.com/org/repo/pull/123", sound=True)
```

## Best Practices

### Session Registration
1. **Register on session start** - Makes you discoverable; enables DMs
2. **Save your session_id** - You'll need it for get_events and publish_event
3. **Include session_id in get_events** - Enables filtering and auto-heartbeat
4. **Poll at natural breakpoints** - Check for messages at session start, before/after major tasks
5. **Unregister on exit** - Keeps the session list clean

### Communication
6. **Use repo channels** - Avoids noise from unrelated projects (prefer `repo:` over `all`)
7. **Keep payloads short** - They're for coordination, not data transfer
8. **DMs auto-notify** - When you send to `session:{id}`, the human gets notified

### Notifications
9. **Notify sparingly** - Only for things the user needs to know
10. **Understand the human-as-router pattern** - Notifications go to the user, not Claude

## How Direct Messages Work (Human as Router)

MCP is request/response only - the server can't push to Claude Code sessions.
When you send a DM, here's what happens:

1. Session A sends: `publish_event("help", "Need review", channel="session:brave-tiger")`
2. Server sends macOS notification: "📨 Message for my-feature (From: auth-work)"
   (Uses terminal-notifier if installed, falls back to osascript)
3. Human sees notification, switches to that terminal
4. Human tells Claude: "check the event bus"
5. Claude polls: `get_events(since_id=last_seen_id, session_id=my_id)` and sees the message

The notification alerts the **human** who routes the message to the correct session.

## Tips

- `get_events` and `publish_event` auto-refresh your heartbeat
- Sessions are auto-cleaned after 7 days of inactivity
- Local sessions are cleaned immediately on PID death (remote sessions use 7-day timeout)
- Events are retained for the last 1000 entries
- The repo name is auto-detected from your working directory
- SessionStart hooks can auto-register you on startup
"""


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
    last = parts[-1] if parts else ""
    return last if last else "unknown"


def _is_pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is still running."""
    if pid is None:
        return True  # Can't check, assume alive
    try:
        os.kill(pid, 0)  # Signal 0 = check if process exists
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # Process exists but we can't signal it


def _auto_heartbeat(session_id: str | None) -> None:
    """Refresh heartbeat for a session if it exists."""
    if session_id and session_id != "anonymous":
        storage.update_heartbeat(session_id, datetime.now())


def _send_notification(title: str, message: str, sound: bool = False) -> bool:
    """Send a system notification. Returns True if successful.

    On macOS, prefers terminal-notifier (supports custom icons) with osascript fallback.
    Icon can be set via EVENT_BUS_ICON environment variable (absolute path to PNG).
    """
    system = platform.system()

    try:
        if system == "Darwin":  # macOS
            # Prefer terminal-notifier for custom icon support
            if shutil.which("terminal-notifier"):
                cmd = [
                    "terminal-notifier",
                    "-title",
                    title,
                    "-message",
                    message,
                    "-group",
                    "event-bus",  # Group notifications together
                    "-sender",
                    "com.apple.Terminal",  # Use Terminal's notification permissions
                ]
                if sound:
                    cmd.extend(["-sound", "default"])

                # Custom icon support via environment variable
                icon_path = os.environ.get("EVENT_BUS_ICON")
                if icon_path and os.path.exists(icon_path):
                    cmd.extend(["-appIcon", icon_path])

                subprocess.run(cmd, check=True, capture_output=True)
                return True

            # Fallback to osascript (no custom icon support)
            script = f'display notification "{message}" with title "{title}"'
            if sound:
                script += ' sound name "default"'
            subprocess.run(
                ["osascript", "-e", script],
                check=True,
                capture_output=True,
            )
            return True

        elif system == "Linux":
            # Check for notify-send
            if shutil.which("notify-send"):
                cmd = ["notify-send", title, message]
                subprocess.run(cmd, check=True, capture_output=True)
                return True
            else:
                logger.warning("notify-send not found on Linux")
                return False

        else:
            logger.warning(f"Notifications not supported on {system}")
            return False

    except subprocess.CalledProcessError as e:
        # Include stderr/stdout for debugging
        stderr = e.stderr.decode() if e.stderr else "no stderr"
        stdout = e.stdout.decode() if e.stdout else "no stdout"
        logger.error(
            f"Notification command failed (exit code {e.returncode}): {e.cmd}\n"
            f"Stdout: {stdout}\n"
            f"Stderr: {stderr}"
        )
        return False


def _dev_notify(tool_name: str, summary: str) -> None:
    """Send a notification in dev mode for tool calls."""
    if os.environ.get("DEV_MODE"):
        _send_notification(f"🔧 {tool_name}", summary)


@mcp.tool()
def register_session(
    name: str,
    machine: str | None = None,
    cwd: str | None = None,
    pid: int | None = None,
) -> dict:
    """Register this Claude session with the event bus.

    Args:
        name: A short name for this session (e.g., branch name, task)
        machine: Machine identifier (defaults to hostname)
        cwd: Working directory (defaults to CWD env var)
        pid: Process ID of the Claude Code client (for session deduplication)

    Returns:
        Session info including assigned session_id

    Tip: Read the resource at event-bus://guide for usage patterns and best practices.
    """
    storage.cleanup_stale_sessions()

    now = datetime.now()
    machine = machine or socket.gethostname()
    cwd = cwd or os.environ.get("PWD", os.getcwd())
    repo = _extract_repo_from_cwd(cwd)

    # Check for existing session with same machine+cwd+pid
    existing = None
    if pid is not None:
        existing = storage.find_session_by_key(machine, cwd, pid)

    if existing:
        # Update existing session
        existing.name = name
        existing.last_heartbeat = now
        storage.add_session(existing)  # INSERT OR REPLACE
        _dev_notify("register_session", f"{name} resumed → {existing.id}")
        return {
            "session_id": existing.id,
            "name": name,
            "machine": machine,
            "cwd": cwd,
            "repo": repo,
            "active_sessions": storage.session_count(),
            "resumed": True,
            "tip": f"You are '{name}' ({existing.id}). Other sessions can DM you at channel 'session:{existing.id}'. Poll get_events() periodically to check for messages.",
        }

    # Create new session with human-readable ID
    session_id = _generate_session_id()
    session = Session(
        id=session_id,
        name=name,
        machine=machine,
        cwd=cwd,
        repo=repo,
        registered_at=now,
        last_heartbeat=now,
        pid=pid,
    )
    storage.add_session(session)

    # Auto-publish registration event
    storage.add_event(
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
        "resumed": False,
        "tip": f"You are '{name}' ({session_id}). Other sessions can DM you at channel 'session:{session_id}'. Poll get_events() periodically to check for messages.",
    }
    _dev_notify("register_session", f"{name} → {session_id}")
    return result


@mcp.tool()
def list_sessions() -> list[dict]:
    """List all active sessions on the event bus.

    Returns:
        List of active sessions with their info
    """
    storage.cleanup_stale_sessions()

    local_hostname = socket.gethostname()
    results = []

    for s in storage.list_sessions():
        # For local sessions with PIDs, check if process is still alive
        is_local = s.machine == local_hostname
        pid_alive = _is_pid_alive(s.pid) if is_local and s.pid else True

        if not pid_alive:
            # Clean up dead session
            storage.delete_session(s.id)
            logger.info(f"Cleaned up dead session {s.id} (PID {s.pid} not running)")
            continue

        results.append(
            {
                "session_id": s.id,
                "name": s.name,
                "machine": s.machine,
                "repo": s.repo,
                "cwd": s.cwd,
                "pid": s.pid,
                "registered_at": s.registered_at.isoformat(),
                "last_heartbeat": s.last_heartbeat.isoformat(),
                "age_seconds": (datetime.now() - s.registered_at).total_seconds(),
            }
        )

    _dev_notify("list_sessions", f"{len(results)} active")
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
    if channel.startswith("session:"):
        parts = channel.split(":", 1)
        if len(parts) != 2 or not parts[1]:
            logger.warning(f"Invalid session channel format: '{channel}'. Expected 'session:<id>'")
        else:
            target_id = parts[1]
            target_session = storage.get_session(target_id)

            if target_session:
                # Get sender info for notification context
                sender_name = "anonymous"
                if session_id:
                    sender_session = storage.get_session(session_id)
                    if sender_session:
                        sender_name = sender_session.name
                    else:
                        logger.warning(
                            f"Sender session '{session_id}' not found when sending DM to {target_id}. "
                            f"Using anonymous as sender name."
                        )

                # Send notification to alert the human
                payload_preview = payload[:50] + "..." if len(payload) > 50 else payload
                try:
                    notification_sent = _send_notification(
                        title=f"📨 Message for {target_session.name}",
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
            else:
                logger.warning(
                    f"Cannot send DM notification: target session '{target_id}' not found. "
                    f"The event was published to channel 'session:{target_id}', but no active session "
                    f"exists with that ID. Session may have expired or ID may be incorrect."
                )

    event = storage.add_event(
        event_type=event_type,
        payload=payload,
        session_id=session_id or "anonymous",
        channel=channel,
    )

    truncated = payload[:50] + "..." if len(payload) > 50 else payload
    _dev_notify("publish_event", f"{event_type} [{channel}] {truncated}")

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
def get_events(since_id: int = 0, limit: int = 50, session_id: str | None = None) -> list[dict]:
    """Get events since a given event ID.

    Events are filtered to channels the session is subscribed to:
    - "all": Broadcasts (everyone receives)
    - "session:{your_id}": Direct messages to you
    - "repo:{your_repo}": Events for your repo
    - "machine:{your_machine}": Events for your machine

    Args:
        since_id: Return events with ID greater than this (default: 0 = all)
        limit: Maximum number of events to return (default: 50)
        session_id: Your session ID (for auto-heartbeat and channel filtering)

    Returns:
        List of events since the given ID
    """
    # Auto-refresh heartbeat when session polls
    _auto_heartbeat(session_id)

    storage.cleanup_stale_sessions()

    # Get channels this session is subscribed to
    channels = _get_implicit_channels(session_id)

    events = [
        {
            "id": e.id,
            "event_type": e.event_type,
            "payload": e.payload,
            "session_id": e.session_id,
            "timestamp": e.timestamp.isoformat(),
            "channel": e.channel,
        }
        for e in storage.get_events(since_id=since_id, limit=limit, channels=channels)
    ]
    _dev_notify("get_events", f"{len(events)} events (since {since_id})")
    return events


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
        _dev_notify("unregister_session", f"{session_id} not found")
        return {"error": "Session not found", "session_id": session_id}

    storage.delete_session(session_id)

    # Publish unregister event
    storage.add_event(
        event_type="session_unregistered",
        payload=f"{session.name} ended on {session.machine}",
        session_id=session_id,
    )

    _dev_notify("unregister_session", f"{session.name} ({session_id})")
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
    success = _send_notification(title, message, sound)
    return {
        "success": success,
        "title": title,
        "message": message,
    }


class RequestLoggingMiddleware:
    """ASGI middleware that logs request and response bodies."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, receive_send):
        if scope["type"] != "http":
            await self.app(scope, receive, receive_send)
            return

        # Collect request body
        body_parts = []

        async def receive_wrapper():
            message = await receive()
            if message["type"] == "http.request":
                body_parts.append(message.get("body", b""))
            return message

        # Collect response body
        response_parts = []
        response_status = None

        async def send_wrapper(message):
            nonlocal response_status
            if message["type"] == "http.response.start":
                response_status = message.get("status")
            elif message["type"] == "http.response.body":
                response_parts.append(message.get("body", b""))
            await receive_send(message)

        await self.app(scope, receive_wrapper, send_wrapper)

        # Log after request completes
        path = scope.get("path", "")
        method = scope.get("method", "")

        if path == "/mcp" and method == "POST":
            request_body = b"".join(body_parts)
            response_body = b"".join(response_parts)

            try:
                req_json = json.loads(request_body) if request_body else {}
                req_method = req_json.get("method", "?")
                req_params = req_json.get("params", {})

                # Format nicely for MCP tool calls
                if req_method == "tools/call":
                    tool_name = req_params.get("name", "?")
                    tool_args = req_params.get("arguments", {})
                    logger.info(f"→ {tool_name}({json.dumps(tool_args)})")
                else:
                    logger.debug(f"→ {req_method}: {json.dumps(req_params)}")

                # Log response
                resp_json = json.loads(response_body) if response_body else {}
                result = resp_json.get("result", resp_json.get("error", {}))

                # Truncate large responses
                result_str = json.dumps(result)
                if len(result_str) > 500:
                    result_str = result_str[:500] + "..."

                logger.info(f"← [{response_status}] {result_str}")

            except json.JSONDecodeError:
                logger.debug(f"→ {method} {path} (non-JSON body)")
                logger.debug(f"← [{response_status}]")


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
