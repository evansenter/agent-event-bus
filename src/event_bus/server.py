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
import socket
import uuid
from datetime import datetime
from typing import Optional

from fastmcp import FastMCP

from event_bus.storage import SQLiteStorage, Session

# Configure logging - only enable DEBUG for our logger, not third-party libs
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("event-bus")
if os.environ.get("DEV_MODE"):
    logger.setLevel(logging.DEBUG)

# Initialize MCP server (stateless_http=True allows resilience to server restarts)
mcp = FastMCP("event-bus", stateless_http=True)

# SQLite-backed storage (persists across restarts)
storage = SQLiteStorage()


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
    storage.cleanup_stale_sessions()

    session_id = str(uuid.uuid4())[:8]
    now = datetime.now()

    machine = machine or socket.gethostname()
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
    storage.add_session(session)

    # Auto-publish registration event
    storage.add_event(
        event_type="session_registered",
        payload=f"{name} started on {machine} in {cwd}",
        session_id=session_id,
    )

    return {
        "session_id": session_id,
        "name": name,
        "machine": machine,
        "cwd": cwd,
        "repo": repo,
        "active_sessions": storage.session_count(),
    }


@mcp.tool()
def list_sessions() -> list[dict]:
    """List all active sessions on the event bus.

    Returns:
        List of active sessions with their info
    """
    storage.cleanup_stale_sessions()

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
        for s in storage.list_sessions()
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
    event = storage.add_event(
        event_type=event_type,
        payload=payload,
        session_id=session_id or "anonymous",
    )

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
    return [
        {
            "id": e.id,
            "event_type": e.event_type,
            "payload": e.payload,
            "session_id": e.session_id,
            "timestamp": e.timestamp.isoformat(),
        }
        for e in storage.get_events(since_id=since_id, limit=limit)
    ]


@mcp.tool()
def heartbeat(session_id: str) -> dict:
    """Send a heartbeat to keep session alive.

    Args:
        session_id: Your session ID from register_session

    Returns:
        Updated session info
    """
    now = datetime.now()
    if not storage.update_heartbeat(session_id, now):
        return {"error": "Session not found", "session_id": session_id}

    return {
        "session_id": session_id,
        "last_heartbeat": now.isoformat(),
        "active_sessions": storage.session_count(),
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
    app = mcp.http_app()

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
